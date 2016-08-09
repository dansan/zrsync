======
ZRsync
======

Continuously synchronize directories from a client to a server with emphasis on low latency (not on large files).

Uses inotify to monitor directories for changes, librsync to reduce bandwidth usage and zeromq for the transport.

Development is done using Python 2.7 and ZeroMQ 4.0. It may or may not run with other versions.
Latest source code can be found `on Github <https://github.com/dansan/zrsync/>`_.

Installation
============

A shell script is provided for easy installation.
Currently only Debian and UCS are supported. Please send me patches to include instructions for your distribution.

.. code-block:: bash

    $ wget https://raw.githubusercontent.com/dansan/zrsync/master/zrsync_install
    $ bash zrsync_install

This will

- install required packages supported by the Linux distribution
- download the zrsync code from github to ``~/.zrsync/zrsync``
- create a Python virtual environment in ``~/.zrsync/virtenv``
- install remaining requirements into the virtual environment
- create shortcuts for running the client and server in ``~/bin``

Running
=======

.. code-block:: bash

    (on target system) $ ~/bin/zrsyncd
    (on source system) $ ~/bin/zrsync /my/local/dir host:/other/dir

The order in which client and server are started does not matter. See ``--help`` for more options:

.. code-block:: bash

    $ ~/bin/zrsync -h
    zrsync client: continuously synchronize a local directory to a [remote] directory.

    Usage:
      zrsync.py [-hinstqvp PORT] <source-dir> <target> [<source-dir> <target>]...

    Arguments:
      source-dir  local directory to sync
      target      server and directory to sync to: [[user@]server:]directory ('user@' automatically enables --ssh)

    Options:
      -h --help              show this help message and exit
      -i --initial-only      only make the inital sync
      -p PORT --port=PORT    port to connect to [default: 24240]
      -n --no-delete         do not delete any files or diretories on the target
      -s --ssh               tunnel connection through SSH. Assumes prior setup of password-less SSH login.
      -l --install           try to install zrsync on target. Automatically enables --ssh.
      -t --shutdown          shutdown server when finished
      -q --quiet             print only errors
      -v --verbose           print debug statements
      --version              show version and exit

****

.. code-block:: bash

    $ ~/bin/zrsyncd -h
    zrsync server: receive continuous updates for a local directory.

    Usage:
      zrsyncd.py [-hqv] [-i IP | --ip=IP] [-p PORT | --port=PORT]

    Options:
      -h --help              show this help message and exit
      -i IP --ip=IP          IP to listen on [default: *]
      -p PORT --port=PORT    port to listen on [default: 24240]
      -q --quiet             print only errors
      -v --verbose           print debug statements
      --version              show version and exit

License
=======

This software is licensed under GNU General Public License v3, see LICENSE.

- `ZeroMQ <http://zeromq.org/>`_ is licensed under the terms of the GNU Lesser General Public License v3.
- `PyZMQ <https://github.com/zeromq/pyzmq>`_ is licensed under the terms of the Modified BSD License as well as the GNU Lesser General Public License v3.
- `librsync <http://librsync.sourcefrog.net/>`_ is licensed under the terms of the GNU Lesser General Public License v2.1.
- `python-librsync <https://github.com/smartfile/python-librsync/>`_ is licensed under the terms of the MIT license.
- `docopt <http://docopt.org/>`_ is licensed under the terms of the MIT license.
- `pyinotify <https://github.com/dsoprea/PyInotify>`_ is licensed under the terms of the GNU General Public License v2.
