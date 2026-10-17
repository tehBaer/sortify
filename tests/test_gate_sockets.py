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
