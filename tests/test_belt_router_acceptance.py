"""Local network fixtures for read-only Belt SSH/LuCI acceptance. No VPS involved."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import unittest
import importlib.util

script=Path(__file__).resolve().parents[1]/"tools/belt_router_acceptance.py"
spec=importlib.util.spec_from_file_location("belt_accept",script)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

@contextlib.contextmanager
def ssh_server(banner=b"SSH-2.0-dropbear_2024.86\\r\\n"):
    sock=socket.socket()
    sock.bind(("127.0.0.1",0))
    sock.listen(1)
    port=sock.getsockname()[1]
    def run():
        try:
            peer,_=sock.accept()
            with peer:
                peer.sendall(banner)
        except OSError:
            pass
        finally:
            sock.close()
    thread=threading.Thread(target=run,daemon=True)
    thread.start()
    try:
        yield port
    finally:
        sock.close()
        thread.join(timeout=2)

@contextlib.contextmanager
def http_server(status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"public login page")
        def log_message(self,*args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

class BeltAcceptTests(unittest.TestCase):
    def test_valid_ssh_and_luci_http_200(self):
        with ssh_server() as ssh, http_server() as http:
            result=m.main(["--ssh-host","127.0.0.1","--ssh-port",str(ssh),
                "--luci-url",f"http://127.0.0.1:{http}/cgi-bin/luci"])
        self.assertEqual(result,0)

    def test_rejects_wrong_ssh_banner(self):
        with ssh_server(b"HTTP/1.1 200 OK\\r\\n") as ssh:
            with self.assertRaisesRegex(ValueError,"SSH-2.0"):
                m.ssh_banner("127.0.0.1",ssh)

    def test_rejects_302_redirect_without_following_it(self):
        with http_server(status=302) as http:
            with self.assertRaisesRegex(ValueError,"HTTP 200"):
                m.luci_status(f"http://127.0.0.1:{http}/cgi-bin/luci")

    def test_rejects_credentials_and_query_url(self):
        for url in ("http://a:pass@127.0.0.1/", "http://127.0.0.1/?token=secret",
                    "file:///etc/passwd","http://127.0.0.1/#frag"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                m.luci_status(url)

if __name__=="__main__":
    unittest.main()
