"""One server, two sockets: the gate depends on uvicorn reporting the port a
connection ARRIVED on in scope["server"]. Proven here end to end on two
ephemeral loopback ports. No Spotify calls: only "/" is requested."""

import asyncio
import http.client
import logging
import threading
import time

import uvicorn

from sortify import app as appmod

from liveguard import assert_not_live_data

assert_not_live_data(appmod.store.dir)

SECRET = "k" * 48


def test_no_public_socket_without_a_good_secret():
    socks = appmod._sockets("127.0.0.1", 0, 1, None)
    try:
        assert len(socks) == 1
    finally:
        for s in socks:
            s.close()


def test_no_public_socket_when_the_port_is_off():
    socks = appmod._sockets("127.0.0.1", 0, 0, SECRET)
    try:
        assert len(socks) == 1
    finally:
        for s in socks:
            s.close()


def test_each_socket_is_told_apart(monkeypatch):
    tail = appmod._bind("127.0.0.1", 0)
    pub = appmod._bind("127.0.0.1", 0)
    monkeypatch.setattr(appmod, "PUBLIC_PORT", pub.getsockname()[1])
    monkeypatch.setenv("SORTIFY_GATE_SECRET", SECRET)
    server = uvicorn.Server(uvicorn.Config(appmod.app, log_level="warning", lifespan="off"))
    t = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[tail, pub])), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)

        def get(sock):
            c = http.client.HTTPConnection("127.0.0.1", sock.getsockname()[1], timeout=5)
            c.request("GET", "/")
            r = c.getresponse()
            return r.status, r.getheader("location"), r.read().decode()

        status, _, body = get(tail)
        assert status == 200 and "rc-button.js" in body
        status, location, _ = get(pub)
        assert status == 302 and location == "/gate/start"
    finally:
        server.should_exit = True
        t.join(timeout=5)


def test_a_successful_bind_logs_both_sockets(caplog):
    """uvicorn.Server.startup() only prints its own "Uvicorn running on ..."
    line when sockets is None; since main() always passes an explicit list,
    _sockets must log each one itself or a successful start is silent in the
    journal."""
    probe = appmod._bind("127.0.0.1", 0)
    public_port = probe.getsockname()[1]
    probe.close()

    with caplog.at_level("INFO", logger="uvicorn.error"):
        socks = appmod._sockets("127.0.0.1", 0, public_port, SECRET)
    try:
        assert len(socks) == 2
        tail_port = socks[0].getsockname()[1]
        pub_port = socks[1].getsockname()[1]
        assert f"{tail_port} (tailnet)" in caplog.text
        assert f"{pub_port} (public, gated)" in caplog.text
    finally:
        for s in socks:
            s.close()


def test_a_refused_public_socket_logs_only_the_tailnet_line(caplog):
    with caplog.at_level("INFO", logger="uvicorn.error"):
        socks = appmod._sockets("127.0.0.1", 0, 1, None)
    try:
        assert len(socks) == 1
        tail_port = socks[0].getsockname()[1]
        assert f"{tail_port} (tailnet)" in caplog.text
        assert "(public" not in caplog.text
        assert "NOT opened" in caplog.text
    finally:
        for s in socks:
            s.close()


def test_a_bind_failure_on_the_public_port_leaves_the_tailnet_socket_up(caplog):
    """A public port already in use (EADDRINUSE — e.g. a dev sortify left
    running on it) must not take the whole process down with it: the
    tailnet route is unrelated and has to keep working."""
    occupied = appmod._bind("127.0.0.1", 0)
    occupied.listen(1)
    occupied_port = occupied.getsockname()[1]
    try:
        with caplog.at_level("ERROR", logger="uvicorn.error"):
            socks = appmod._sockets("127.0.0.1", 0, occupied_port, SECRET)
        try:
            assert len(socks) == 1
            assert "NOT opened" in caplog.text
            assert str(occupied_port) in caplog.text
        finally:
            for s in socks:
                s.close()
    finally:
        occupied.close()


def test_main_configures_logging_before_opening_sockets(monkeypatch):
    """caplog forces the "uvicorn.error" logger's level, which would hide an
    ordering bug: it is uvicorn.Config.__init__ -> configure_logging() that
    actually raises that logger from its default WARNING to INFO, and
    main() must build the Config before calling _sockets(), or the
    "listening on ..." lines are filtered out in a real process. This test
    does not force the level — it resets it to NOTSET and reads real
    emitted records, so it fails if main() opens the sockets first.

    One wrinkle verified against uvicorn 0.52.4: configure_logging() runs
    logging.config.dictConfig(), and dictConfig resets a configured
    logger's *handlers* too — "uvicorn.error" has no "handlers" key in
    uvicorn's LOGGING_CONFIG, so dictConfig clears it to [] every time a
    Config is built, wiping a handler attached beforehand regardless of
    ordering. A handler attached before main() runs therefore proves
    nothing (it fails in both the broken and the fixed order). So this
    hooks Config.__init__ to (re-)attach the collector the instant
    configure_logging() has finished — the earliest point a handler can
    survive — leaving the ordering bug as the only thing that can still
    make the assertions below fail: if _sockets() runs before any Config
    is built, neither the handler nor the raised level exist yet, and the
    "listening on ..." calls are lost for real, not because of a level we
    forced. uvicorn's LOGGING_CONFIG also marks "uvicorn" non-propagating,
    so caplog's root handler would never see these records either way."""
    logger = logging.getLogger("uvicorn.error")
    previous_level = logger.level
    logger.setLevel(logging.NOTSET)

    records = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collector()
    original_config_init = uvicorn.Config.__init__

    def _init_then_attach(self, *a, **kw):
        original_config_init(self, *a, **kw)
        logger.addHandler(handler)

    monkeypatch.setattr(uvicorn.Config, "__init__", _init_then_attach)

    probe = appmod._bind("127.0.0.1", 0)
    free_port = probe.getsockname()[1]
    probe.close()

    monkeypatch.setenv("SORTIFY_HOST", "127.0.0.1")
    monkeypatch.setenv("SORTIFY_PORT", "0")
    monkeypatch.setattr(appmod, "PUBLIC_PORT", free_port)
    monkeypatch.setenv("SORTIFY_GATE_SECRET", SECRET)

    recorded = {}

    def _fake_run(self, sockets=None):
        recorded["sockets"] = sockets

    monkeypatch.setattr(uvicorn.Server, "run", _fake_run)

    try:
        appmod.main()
        messages = [r.getMessage() for r in records]
        assert any("(tailnet)" in m for m in messages)
        assert any("(public, gated)" in m for m in messages)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        for s in recorded.get("sockets") or []:
            s.close()
