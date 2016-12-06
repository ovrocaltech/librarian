# -*- mode: python; coding: utf-8 -*-
# Copyright 2016 the HERA Collaboration
# Licensed under the BSD License.

"Files."

from __future__ import absolute_import, division, print_function, unicode_literals

__all__ = str('''
File
FileInstance
FileEvent
''').split ()

import datetime, json, os.path, re
from flask import flash, redirect, render_template, url_for

from . import app, db
from .dbutil import NotNull
from .webutil import ServerError, json_api, login_required, optional_arg, required_arg
from .observation import Observation
from .store import Store


class File (db.Model):
    """A File describes a data product generated by HERA.

    The information described in a File structure never changes, and is
    universal between Librarians. Actual "instances" of files come and go, but
    a File record should never be deleted. The only exception to this is the
    "source" column, which is Librarian-dependent.

    A File may represent an actual flat file or a directory tree. The latter
    use case is important for MIRIAD data files, which are directories, and
    which we want to store in their native form for rapid analysis.

    File names are unique. Here, the "name" is a Unix 'basename', i.e. it
    contains no directory components or slashes. Every new file must have a
    unique new name.

    """
    __tablename__ = 'file'

    name = db.Column (db.String (256), primary_key=True)
    type = NotNull (db.String (32))
    create_time = NotNull (db.DateTime) # rounded to integer seconds
    obsid = db.Column (db.BigInteger, db.ForeignKey (Observation.obsid), nullable=False)
    size = NotNull (db.BigInteger)
    md5 = NotNull (db.String (32))

    source = NotNull (db.String (64))
    observation = db.relationship ('Observation', back_populates='files')
    instances = db.relationship ('FileInstance', back_populates='file')
    events = db.relationship ('FileEvent', back_populates='file')

    def __init__ (self, name, type, obsid, source, size, md5, create_time=None):
        if create_time is None:
            # We round our times to whole seconds so that they can be
            # accurately represented as integer Unix times, just in case
            # floating-point rounding could sneak in as an issue.
            create_time = datetime.datetime.utcnow ().replace (microsecond=0)

        from hera_librarian import utils
        md5 = utils.normalize_and_validate_md5 (md5)

        self.name = name
        self.type = type
        self.create_time = create_time
        self.obsid = obsid
        self.source = source
        self.size = size
        self.md5 = md5
        self._validate ()


    def _validate (self):
        """Check that this object's fields follow our invariants.

        """
        from hera_librarian import utils

        if '/' in self.name:
            raise ValueError ('illegal file name "%s": names may not contain "/"' % self.name)

        utils.normalize_and_validate_md5 (self.md5)

        if not (self.size >= 0): # catches NaNs, just in case ...
            raise ValueError ('illegal size %d of file "%s": negative' % (self.size, self.name))


    @classmethod
    def get_inferring_info (cls, store, store_path, source_name, info=None):
        """Get a File instance based on a file currently located in a store. We infer
        the file's properties and those of any dependent database records
        (Observation, ObservingSession), which means that we can only do this
        for certain kinds of files whose formats we understand.

        If new File and Observation records need to be created in the DB, that
        is done. If *info* is given, we use it; otherwise we SSH into the
        store to gather the info ourselves.

        """
        parent_dirs = os.path.dirname (store_path)
        name = os.path.basename (store_path)

        prev = cls.query.get (name)
        if prev is not None:
            # If there's already a record for this File name, then its corresponding
            # Observation etc must already be available. Let's leave well enough alone:
            return prev

        # Darn. We're going to have to create the File, and maybe its
        # Observation too. Get to it.

        if info is None:
            try:
                info = store.get_info_for_path (store_path)
            except Exception as e:
                raise ServerError ('cannot register %s:%s: %s', store.name, store_path, e)

        size = required_arg (info, int, 'size')
        md5 = required_arg (info, unicode, 'md5')
        type = required_arg (info, unicode, 'type')
        lst = required_arg (info, float, 'lst')

        from .observation import Observation
        obsid = required_arg (info, int, 'obsid')
        obs = Observation.query.get (obsid)

        if obs is None:
            start_jd = required_arg (info, float, 'start_jd')
            db.session.add (Observation (obsid, start_jd, None, lst))

        inst = File (name, type, obsid, source_name, size, md5)
        db.session.add (inst)
        db.session.commit ()
        return inst


    @property
    def create_time_unix (self):
        import calendar
        return calendar.timegm (self.create_time.timetuple ())


    def to_dict (self):
        """Note that 'source' is not a propagated quantity."""
        return dict (
            name = self.name,
            type = self.type,
            create_time = self.create_time_unix,
            obsid = self.obsid,
            size = self.size,
            md5 = self.md5
        )


    @classmethod
    def from_dict (cls, source, info):
        name = required_arg (info, unicode, 'name')
        type = required_arg (info, unicode, 'type')
        ctime_unix = required_arg (info, int, 'create_time')
        obsid = required_arg (info, int, 'obsid')
        size = required_arg (info, int, 'size')
        md5 = required_arg (info, unicode, 'md5')
        return cls (name, type, obsid, source, size, md5, datetime.datetime.fromtimestamp (ctime_unix))


    def make_generic_event (self, type, **kwargs):
        """Create a new FileEvent record relating to this file. The new event is not
        added or committed to the database.

        """
        return FileEvent (self.name, type, kwargs)


    def make_instance_creation_event (self, instance, store):
        return self.make_generic_event ('create_instance',
                                        store_name=store.name,
                                        parent_dirs=instance.parent_dirs)


    def make_copy_launched_event (self, connection_name, remote_store_path):
        return self.make_generic_event ('launch_copy',
                                        connection_name=connection_name,
                                        remote_store_path=remote_store_path)


    def make_copy_finished_event (self, connection_name, remote_store_path,
                                  error_code, error_message, duration=None,
                                  average_rate=None):
        extras = {}

        if duration is not None:
            extras['duration'] = duration # seconds
        if average_rate is not None:
            extras['average_rate'] = average_rate # kilobytes/sec

        return self.make_generic_event ('copy_finished',
                                        connection_name=connection_name,
                                        remote_store_path=remote_store_path,
                                        error_code=error_code,
                                        error_message=error_message,
                                        **extras)


class FileInstance (db.Model):
    """A FileInstance is a copy of a File that lives on one of this Librarian's
    stores.

    Because the File record knows the key attributes of the file that we're
    instantiating (size, MD5 sum), a FileInstance record only needs to keep
    track of the location of this instance: its store, its parent directory,
    and the file name (which, because File names are unique, is a foreign key
    into the File table).

    Even though File names are unique, for organizational purposes they are
    sorted into directories when instantiated in actual stores. In current
    practice this is generally done by JD although this is not baked into the
    design.

    """
    __tablename__ = 'file_instance'

    store = db.Column (db.BigInteger, db.ForeignKey (Store.id), primary_key=True)
    parent_dirs = db.Column (db.String (128), primary_key=True)
    name = db.Column (db.String (256), db.ForeignKey (File.name), primary_key=True)
    file = db.relationship ('File', back_populates='instances')
    store_object = db.relationship ('Store', back_populates='instances')

    def __init__ (self, store_obj, parent_dirs, name):
        if '/' in name:
            raise ValueError ('illegal file name "%s": names may not contain "/"' % name)

        self.store = store_obj.id
        self.parent_dirs = parent_dirs
        self.name = name

    @property
    def store_name (self):
        return self.store_object.name

    @property
    def store_path (self):
        import os.path
        return os.path.join (self.parent_dirs, self.name)

    def full_path_on_store (self):
        import os.path
        return os.path.join (self.store_object.path_prefix, self.parent_dirs, self.name)


class FileEvent (db.Model):
    """A FileEvent is a something that happens to a File on this Librarian.

    Note that events are per-File, not per-FileInstance. One reason for this
    is that FileInstance records may get deleted, and we want to be able to track
    history even after that happens.

    On the other hand, FileEvents are private per Librarian. They are not
    synchronized from one Librarian to another and are not globally unique.

    The nature of a FileEvent payload is defined by its type. We suggest
    JSON-encoded text. The payload is limited to 512 bytes so there's only so
    much you can carry.

    """
    __tablename__ = 'file_event'

    id = db.Column (db.BigInteger, primary_key=True)
    name = db.Column (db.String (256), db.ForeignKey (File.name))
    time = NotNull (db.DateTime)
    type = db.Column (db.String (64))
    payload = db.Column (db.Text)
    file = db.relationship ('File', back_populates='events')

    def __init__ (self, name, type, payload_struct):
        if '/' in name:
            raise ValueError ('illegal file name "%s": names may not contain "/"' % name)

        self.name = name
        self.time = datetime.datetime.utcnow ().replace (microsecond=0)
        self.type = type
        self.payload = json.dumps (payload_struct)


    @property
    def payload_json (self):
        return json.loads (self.payload)


# RPC endpoints

@app.route ('/api/create_file_event', methods=['GET', 'POST'])
@json_api
def create_file_event (args, sourcename=None):
    """Create a FileEvent record for a File.

    We enforce basically no structure on the event data.

    """
    file_name = required_arg (args, unicode, 'file_name')
    type = required_arg (args, unicode, 'type')
    payload = required_arg (args, dict, 'payload')

    file = File.query.get (file_name)
    if file is None:
        raise ServerError ('no known file "%s"', file_name)

    event = file.make_generic_event (type, **payload)
    db.session.add (event)
    db.session.commit ()
    return {}


@app.route ('/api/locate_file_instance', methods=['GET', 'POST'])
@json_api
def locate_file_instance (args, sourcename=None):
    """Tell the caller where to find an instance of the named file.

    """
    file_name = required_arg (args, unicode, 'file_name')

    file = File.query.get (file_name)
    if file is None:
        raise ServerError ('no known file "%s"', file_name)

    for inst in file.instances:
        return {
            'full_path_on_store': inst.full_path_on_store (),
            'store_name': inst.store_name,
            'store_path': inst.store_path,
            'store_ssh_host': inst.store_object.ssh_host,
        }

    raise ServerError ('no instances of file "%s" on this librarian', file_name)


# Web user interface

@app.route ('/files/<string:name>')
@login_required
def specific_file (name):
    file = File.query.get (name)
    if file is None:
        flash ('No such file "%s" known' % name)
        return redirect (url_for ('index'))

    instances = list (FileInstance.query.filter (FileInstance.name == name))
    events = sorted (file.events, key=lambda e: e.time, reverse=True)

    return render_template (
        'file-individual.html',
        title='%s File %s' % (file.type, file.name),
        file=file,
        instances=instances,
        events=events,
    )
