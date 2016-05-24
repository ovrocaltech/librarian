# -*- mode: python; coding: utf-8 -*-
# Copyright 2016 the HERA Collaboration
# Licensed under the BSD License.

"""The way that Flask is designed, we have to read our configuration and
initialize many things on module import, which is a bit lame. There are
probably ways to work around that but things work well enough as is.

"""
from __future__ import absolute_import, division, print_function, unicode_literals

import sys


def _initialize ():
    import json, os.path
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy

    config_path = os.environ.get ('LIBRARIAN_CONFIG_PATH', 'server-config.json')
    with open (config_path) as f:
        config = json.load (f)

    if 'SECRET_KEY' not in config:
        print ('cannot start server: must define the Flask "secret key" as the item '
               '"SECRET_KEY" in "server-config.json"', file=sys.stderr)
        sys.exit (1)

    tf = os.path.join (os.path.dirname (os.path.abspath (__file__)), 'templates')
    app = Flask ('librarian', template_folder=tf)
    app.config.update (config)
    db = SQLAlchemy (app)
    return app, db

app, db = _initialize ()


# We have to manually import the modules that implement services. It's not
# crazy to worry about circular dependency issues, but everything will be all
# right.

from . import webutil
from . import observation
from . import store
from . import file
from . import bgtasks
from . import search
from . import misc


# Finally ...

def commandline (argv):
    server = app.config.get ('server', 'flask')
    host = app.config.get ('host', None)
    port = app.config.get ('port', 21106)
    debug = app.config.get ('flask-debug', False)

    if host is None:
        print ('note: no "host" set in configuration; server will not be remotely accessible',
               file=sys.stderr)

    initdb = app.config.get ('initialize-database', False)
    if initdb:
        init_database ()

    if server == 'flask':
        print ('note: using "flask" server, so background operations will not work',
               file=sys.stderr)
        app.run (host=host, port=port, debug=debug)
    elif server == 'tornado':
        from tornado.wsgi import WSGIContainer
        from tornado.httpserver import HTTPServer
        from tornado.ioloop import IOLoop
        http_server = HTTPServer(WSGIContainer(app))
        http_server.listen (port, address=host)
        IOLoop.instance ().start ()
    else:
        print ('error: unknown server type %r' % server, file=sys.stderr)
        sys.exit (1)

    bgtasks.maybe_wait_for_threads_to_finish ()


def init_database ():
    """NB: make sure this code doesn't blow up if invoked on an
    already-initialized database.

    """
    db.create_all ()

    from .store import Store

    for name, cfg in app.config.get ('add-stores', {}).iteritems ():
        prev = Store.query.filter (Store.name == name).first ()
        if prev is None:
            store = Store (name, cfg['path_prefix'], cfg['ssh_host'])
            store.http_prefix = cfg.get ('http_prefix')
            store.available = cfg.get ('available', True)
            db.session.add (store)

    db.session.commit ()
