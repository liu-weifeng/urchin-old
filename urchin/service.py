import os
import random
import sys

from urchin import wsgi


class Service(object):

    def __init__(self, periodic_enable=None):
        pass

    def start(self):



        # start wsgi

        pass

    def stop(self):
        pass

    @classmethod
    def create(cls, periodic_enable=None):

        return cls()

def _load_app(name):

    if name == 'compute':
        return


class WSGIService(object):

    def __init__(self, name, use_ssl=False, max_url_len=None):

        self.app = _load_app(name)
        self.host='0.0.0.0'
        self.port='8888'
        self.use_ssl=use_ssl

        self.server = wsgi.Server(name,
                                  self.app,
                                  host=self.host,
                                  port=self.port,
                                  use_ssl=self.use_ssl,
                                  max_url_len=max_url_len)

    @classmethod
    def create(cls, name):
        return cls(name)

    def start(self):
        self.server.start()

    def wait(self):
        self.server.wait()
