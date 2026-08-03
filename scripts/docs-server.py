"""HTTP-сервер портала docs/ — крутится на самом принтере, порт 8001.

Заливается на принтер скриптом scripts/serve-docs.sh в /tmp/docsrv/ и запускается
там же. В репозитории лежит потому, что /tmp на принтере — tmpfs: он исчезает при
перезагрузке, и восстанавливать сервер надо из git, а не из памяти прошлой сессии.

Единственное отличие от `python3 -m http.server` — заголовки, запрещающие кэш.
Без них браузер у станка показывает вчерашнюю версию страницы, и приходится
приписывать к URL `?v=N`, чтобы его переубедить. С no-store URL остаётся голым:
http://<принтер>:8001/ — просто F5, и это точно свежая версия.
"""
import http.server
import os
import socketserver

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DOCS_PORT", "8001"))

os.chdir(ROOT)


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Orange Pi One: 512 МБ RAM и journald на карте памяти. Лог сервера
        # предпросмотра туда лить незачем — оставляем только строку на запрос.
        super().log_message(fmt, *args)


socketserver.TCPServer.allow_reuse_address = True

with socketserver.TCPServer(("0.0.0.0", PORT), NoCacheHandler) as httpd:
    print("docs portal serving %s on 0.0.0.0:%d" % (ROOT, PORT), flush=True)
    httpd.serve_forever()
