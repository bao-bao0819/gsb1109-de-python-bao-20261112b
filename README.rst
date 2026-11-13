=====
polib
=====

|pypi-version| |pypi-stats| |build-status-image| |codecov-image| |documentation-status-image| |py-versions|

Overview
--------

polib is a library to manipulate, create, modify gettext files (pot, po and mo
files). You can load existing files, iterate through it's entries, add, modify
entries, comments or metadata, etc... or create new po files from scratch.

polib supports out of the box any version of python ranging from 2.7 to latest
3.X version.

polib is pretty stable now and is used by many 
`opensource projects <http://polib.readthedocs.org/en/latest/projects.html>`_.

The project code and bugtracker is hosted on 
`Github <https://github.com/izimobil/polib/>`_.

polib is generously documented, you can `browse the documentation online 
<http://polib.readthedocs.org/>`_, a good start is to read 
`the quickstart guide  <http://polib.readthedocs.org/en/latest/quickstart.html>`_.


Installation
~~~~~~~~~~~~

Just use ``pip``:

.. code:: bash

    $ pip install polib


Basic example
~~~~~~~~~~~~~

.. code:: python

    import polib

    pofile = polib.pofile('/path/to/pofile.po')

    for entry in pofile:
        print(entry.msgid, entry.msgstr)


Building a byte-bounded slim catalog (``moslim``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Devices sometimes only allow a fixed number of bytes for the compiled
``.mo`` file. ``moslim.py`` builds a loadable catalog that is guaranteed to
fit: required entries are kept first (a plural message always ships with
all of its branches), then the remaining entries are admitted by offline
usage count while the real compiled size stays within the limit.

.. code:: bash

    $ python moslim.py app.po slim.mo --max-bytes 2048 \
        --required "Save" --required "menu\x04Open" --usage usage.txt

The ``usage.txt`` file holds one ``key = count`` line per msgid (contextual
keys use the literal ``\x04`` separator). If the required closure alone is
bigger than the budget, nothing is written and its compiled size is printed
instead. The same logic is available programmatically:

.. code:: python

    import moslim

    entries, report = moslim.build_slim_catalog(
        polib.pofile('app.po'), max_bytes=2048,
        required_keys=['Save'], usage={'Cancel': 12},
    )
    if entries is None:
        print('closure alone needs', report['required_size'], 'bytes')


.. |build-status-image| image:: https://api.travis-ci.com/izimobil/polib.svg?branch=master
   :target: https://app.travis-ci.com/github/izimobil/polib
   :alt: Travis build

.. |codecov-image| image:: https://codecov.io/gh/izimobil/polib/branch/master/graph/badge.svg
  :target: https://codecov.io/gh/izimobil/polib

.. |pypi-version| image:: https://img.shields.io/pypi/v/polib.svg
   :target: https://pypi.python.org/pypi/polib
   :alt: Pypi version

.. |pypi-stats| image:: https://img.shields.io/pypi/dm/polib.svg
   :target: https://pypistats.org/packages/polib
   :alt: Pypi downloads

.. |documentation-status-image| image:: https://readthedocs.org/projects/polib/badge/?version=latest
   :target: http://polib.readthedocs.io/en/latest/?badge=latest
   :alt: Documentation Status

.. |py-versions| image:: https://img.shields.io/pypi/pyversions/polib.svg
   :target: https://img.shields.io/pypi/pyversions/polib.svg
   :alt: Python versions
