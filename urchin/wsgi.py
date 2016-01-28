import eventlet
from eventlet import wsgi
from urchin import util


class Server(object):

    def __init__(self, name, app, host, port, use_ssl=None, max_url_len=None):

        self.host = host
        self.port = port
        self.app = app
        self._pool = eventlet.GreenPool(100)

    def start(self):
        wsgi.server(eventlet.listen(('', self.port)), self.app)
        self._server = util.spawn()

    def wait(self):

        try:
            if self._server is not None:
                self._pool.waitall()
                self._server.wait()
        except:
            pass


