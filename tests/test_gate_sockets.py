"""One server, two sockets: the gate depends on uvicorn reporting the port a
connection ARRIVED on in scope["server"]. Proven here end to end on two
ephemeral loopback ports. No Spotify calls: only "/" is requested."""

import asyncio
import http.client
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
