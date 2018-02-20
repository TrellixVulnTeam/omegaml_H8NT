"""
Native storage for OmegaML using mongodb as the storage layer

An OmegaStore instance is a MongoDB database. It has at least the
metadata collection which lists all objects stored in it. A metadata
document refers to the following types of objects (metadata.kind):

* pandas.dfrows - a Pandas DataFrame stored as a collection of rows
* sklearn.joblib - a scikit learn estimator/pipline dumped using joblib.dump()
* python.data - an arbitrary python dict, tuple, list stored as a document

Note that storing Pandas and scikit learn objects requires the availability
of the respective packages. If either can not be imported, the OmegaStore
degrades to a python.data store only. It will still .list() and get() any
object, however reverts to pure python objects. In this case it is up
to the client to convert the data into an appropriate format for processing.

Pandas and scikit-learn objects can only be stored if these packages are
availables. put() raises a TypeError if you pass such objects and these
modules cannot be loaded.

All data are stored within the same mongodb, in per-object collections 
as follows:

    * .metadata
        all metadata. each object is one document, 
        See **omegaml.documents.Metadata** for details
    * .<bucket>.files
        this is the GridFS instance used to store
        blobs (models, numpy, hdf). The actual file name
        will be <prefix>/<name>.<ext>, where ext is 
        optionally generated by put() / get(). 
    * .<bucket>.<prefix>.<name>.data
        every other dataset is stored in a separate
        collection (dataframes, dicts, lists, tuples).
        Any forward slash in prefix is ignored (e.g. 'data/' 
        becomes 'data')

    DataFrames by default are stored in their own collection, every
    row becomes a document. To store dataframes as a binary file,
    use `put(...., as_hdf=True).` `.get()` will always return a dataframe.

    Python dicts, lists, tuples are stored as a single document with
    a `.data` attribute holding the JSON-converted representation. `.get()`
    will always return the corresponding python object of .data. 

    Models are joblib.dump()'ed and ziped prior to transferring into
    GridFs. .get() will always unzip and joblib.load() before returning
    the model. Note this requires that the process using .get() supports
    joblib as well as all python classes referred to. If joblib is not
    supported, .get() returns a file-like object.

    The .metadata entry specifies the format used to store each
    object as well as it's location:

    * metadata.kind
        the type of object
    * metadata.name
        the name of the object, as given on put()
    * metadata.gridfile
        the gridfs object (if any, null otherwise)
    * metadata.collection
        the name of the collection
    * metadata.attributes 
        arbitrary custom attributes set in 
        put(attributes=obj). This is used e.g. by 
        OmegaRuntime's fit() method to record the
        data used in the model's training.

    **.put()** and **.get()** use helper methods specific to the type in
    object's type and metadata.kind, respectively. In the future 
    a plugin system will enable extension to other types. 
"""
from __future__ import absolute_import

from datetime import datetime
from fnmatch import fnmatch
import os
import re
import tempfile
from uuid import uuid4

import gridfs
import mongoengine
from mongoengine.connection import register_connection, disconnect,\
    get_connection, connect
from mongoengine.errors import DoesNotExist
from mongoengine.fields import GridFSProxy
from six import iteritems
import six

from omegacommon.util import extend_instance
from omegaml import signals
from omegaml.store.fastinsert import fast_insert
from omegaml.util import unravel_index, restore_index, make_tuple, jsonescape,\
    cursor_to_dataframe

from ..documents import Metadata
from ..util import (is_estimator, is_dataframe, is_ndarray, is_spark_mllib,
                    settings as omega_settings, urlparse, is_series)


class OmegaStore(object):

    """
    The storage backend for models and data
    """

    def __init__(self, mongo_url=None, bucket=None, prefix=None, kind=None):
        """
        :param mongo_url: the mongourl to use for the gridfs
        :param bucket: the mongo collection to use for gridfs
        :param prefix: the path prefix for files. defaults to blank
        :param kind: the kind or list of kinds to limit this store to 
        """
        self.defaults = omega_settings()
        self.mongo_url = mongo_url or self.defaults.OMEGA_MONGO_URL
        self.bucket = bucket or self.defaults.OMEGA_MONGO_COLLECTION
        self._fs = None
        self.tmppath = self.defaults.OMEGA_TMP
        self.prefix = prefix or ''
        self.force_kind = kind
        # don't initialize db here to avoid using the default settings
        # otherwise Metadata will already have a connection and not use
        # the one provided in override_settings
        self._db = None
        # add backends and mixins
        self.apply_mixins()
        # register backends
        self.register_backends()

    @property
    def mongodb(self):
        """
        Returns a mongo database object
        """
        if self._db is not None:
            return self._db
        # parse salient parts of mongourl, e.g.
        # mongodb://user:password@host/dbname
        self.parsed_url = urlparse.urlparse(self.mongo_url)
        self.database_name = self.parsed_url.path[1:]
        host = self.parsed_url.netloc
        username, password = None, None
        if '@' in host:
            creds, host = host.split('@', 1)
            if ':' in creds:
                username, password = creds.split(':')
        # connect via mongoengine
        # note this uses a MongoClient in the background, with pooled
        # connections. there are multiprocessing issues with pymongo:
        # http://api.mongodb.org/python/3.2/faq.html#using-pymongo-with-multiprocessing
        # connect=False is due to https://jira.mongodb.org/browse/PYTHON-961
        # this defers connecting until the first access
        # serverSelectionTimeoutMS=2500 is to fail fast, the default is 30000
        # FIXME use an instance specific alias. requires that every access
        #       to Metadata is configured correctly. this to avoid sharing
        #       inadevertedly between threads and processes.
        #alias = 'omega-{}'.format(uuid4().hex)
        alias = 'omega'
        # always disconnect before registering a new connection because
        # connect forgets all connection settings upon disconnect WTF?!
        disconnect(alias)
        connection = connect(alias=alias, db=self.database_name,
                             host=host,
                             username=username,
                             password=password,
                             connect=False,
                             serverSelectionTimeoutMS=2500)
        self._db = getattr(connection, self.database_name)
        # mongoengine 0.15.0 connection setup is seriously broken -- it does
        # not remember username/password on authenticated connections
        # so we reauthenticate here
        if username and password:
            self._db.authenticate(username, password)
        return self._db

    @property
    def fs(self):
        """
        Retrieve a gridfs instance using url and collection provided

        :return: a gridfs instance
        """
        if self._fs is not None:
            return self._fs
        try:
            self._fs = gridfs.GridFS(self.mongodb, collection=self.bucket)
        except Exception as e:
            raise e
        return self._fs

    def metadata(self, name=None, bucket=None, prefix=None, version=-1):
        """
        Returns a metadata document for the given entry name

        FIXME: version attribute does not do anything
        FIXME: metadata should be stored in a bucket-specific collection
        to enable access control, see https://docs.mongodb.com/manual/reference/method/db.createRole/#db.createRole
        """
        db = self.mongodb
        fs = self.fs
        prefix = prefix or self.prefix
        bucket = bucket or self.bucket
        # Meta is to silence lint on import error
        Meta = Metadata
        return Meta.objects(name=name, prefix=prefix, bucket=bucket).first()

    def make_metadata(self, name, kind, bucket=None, prefix=None, **kwargs):
        """
        create or update a metadata object

        this retrieves a Metadata object if it exists given the kwargs. Only
        the name, prefix and bucket arguments are considered

        for existing Metadata objects, the attributes kw is treated as follows:

        * attributes=None, the existing attributes are left as is
        * attributes={}, the attributes value on an existing metadata object
          is reset to the empty dict
        * attributes={ some : value }, the existing attributes are updated

        For new metadata objects, attributes defaults to {} if not specified,
        else is set as provided.    

        :param name: the object name
        :param bucket: the bucket, optional, defaults to self.bucket 
        :param prefix: the prefix, optional, defaults to self.prefix

        """
        # TODO kept _make_metadata for backwards compatibility.
        return self._make_metadata(name, bucket=bucket, prefix=prefix,
                                   kind=kind, **kwargs)

    def _make_metadata(self, name=None, bucket=None, prefix=None, **kwargs):
        """
        create or update a metadata object

        this retrieves a Metadata object if it exists given the kwargs. Only
        the name, prefix and bucket arguments are considered

        for existing Metadata objects, the attributes kw is treated as follows:

        * attributes=None, the existing attributes are left as is
        * attributes={}, the attributes value on an existing metadata object
        is reset to the empty dict
        * attributes={ some : value }, the existing attributes are updated

        For new metadata objects, attributes defaults to {} if not specified,
        else is set as provided.    

        :param name: the object name
        :param bucket: the bucket, optional, defaults to self.bucket 
        :param prefix: the prefix, optional, defaults to self.prefix
        """
        bucket = bucket or self.bucket
        prefix = prefix or self.prefix
        meta = self.metadata(name=name,
                             prefix=prefix,
                             bucket=bucket)
        if meta:
            for k, v in six.iteritems(kwargs):
                if k == 'attributes' and v is not None and len(v) > 0:
                    previous = getattr(meta, k, {})
                    previous.update(v)
                    setattr(meta, k, previous)
                elif k == 'attributes' and v is not None and len(v) == 0:
                    setattr(meta, k, {})
                elif k == 'attributes' and v is None:
                    # ignore non specified attributes
                    continue
                else:
                    # by default set whatever attribute is provided
                    setattr(meta, k, v)
        else:
            meta = Metadata(name=name, bucket=bucket, prefix=prefix,
                            **kwargs)
        return meta

    def _drop_metadata(self, name=None, **kwargs):
        # internal method to delete meta data of an object
        meta = self.metadata(name, **kwargs)
        if meta is not None:
            meta.delete()

    def collection(self, name=None):
        """
        Returns a mongo db collection as a datastore

        :param name: the collection to use. if none defaults to the
            collection name given on instantiation. the actual collection name
            used is always prefix + name + '.data'
        """
        collection = self._get_obj_store_key(name, '.datastore')
        collection = collection.replace('..', '.')
        try:
            datastore = getattr(self.mongodb, collection)
        except Exception as e:
            raise e
        return datastore

    def apply_mixins(self):
        """
        apply mixins in defaults.OMEGA_STORE_MIXINS
        """
        for mixin in self.defaults.OMEGA_STORE_MIXINS:
            extend_instance(self, mixin)

    def register_backends(self):
        """
        register backends in defaults.OMEGA_STORE_BACKENDS
        """
        for kind, backend in six.iteritems(self.defaults.OMEGA_STORE_BACKENDS):
            self.register_backend(kind, backend)

    def register_backend(self, kind, backend):
        """
        register a backend class

        :param kind: (str) the backend kind
        :param backend: (class) the backend class 
        """
        self.defaults.OMEGA_STORE_BACKENDS[kind] = backend
        if kind not in Metadata.KINDS:
            Metadata.KINDS.append(kind)
        return self

    def register_mixin(self, mixincls):
        """
        register a mixin class

        :param mixincls: (class) the mixin class 
        """
        self.defaults.OMEGA_STORE_MIXINS.append(mixincls)
        extend_instance(self, mixincls)
        return self

    def put(self, obj, name, attributes=None, **kwargs):
        """
        Stores an objecs, store estimators, pipelines, numpy arrays or
        pandas dataframes
        """
        for kind, backend_cls in six.iteritems(self.defaults.OMEGA_STORE_BACKENDS):
            if backend_cls.supports(obj, name, attributes=attributes, **kwargs):
                backend = self.get_backend_bykind(kind)
                return backend.put(obj, name, attributes=attributes, **kwargs)
        if is_estimator(obj):
            backend = self.get_backend_bykind(Metadata.SKLEARN_JOBLIB)
            signals.dataset_put.send(sender=None, name=name)
            return backend.put_model(obj, name, attributes)
        elif is_spark_mllib(obj):
            backend = self.get_backend_bykind(Metadata.SKLEARN_JOBLIB)
            signals.dataset_put.send(sender=None, name=name)
            return backend.put_model(obj, name, attributes, **kwargs)
        elif is_dataframe(obj) or is_series(obj):
            groupby = kwargs.get('groupby')
            if obj.empty:
                from warnings import warn
                warn(
                    'Provided dataframe is empty, ignoring it, doing nothing here!')
                return None
            if kwargs.pop('as_hdf', False):
                return self.put_dataframe_as_hdf(
                    obj, name, attributes, **kwargs)
            elif groupby:
                return self.put_dataframe_as_dfgroup(
                    obj, name, groupby, attributes)
            append = kwargs.get('append', None)
            timestamp = kwargs.get('timestamp', None)
            index = kwargs.get('index', None)
            return self.put_dataframe_as_documents(
                obj, name, append, attributes, index, timestamp)
        elif is_ndarray(obj):
            return self.put_ndarray_as_hdf(obj, name,
                                           attributes=attributes,
                                           **kwargs)
        elif isinstance(obj, (dict, list, tuple)):
            if kwargs.pop('as_hdf', False):
                self.put_pyobj_as_hdf(obj, name,
                                      attributes=attributes, **kwargs)
            return self.put_pyobj_as_document(obj, name,
                                              attributes=attributes,
                                              **kwargs)
        else:
            raise TypeError('type %s not supported' % type(obj))

    def put_dataframe_as_documents(self, obj, name, append=None,
                                   attributes=None, index=None,
                                   timestamp=None):
        """
        store a dataframe as a row-wise collection of documents

        :param obj: the dataframe to store
        :param name: the name of the item in the store
        :param append: if False collection will be dropped before inserting,
           if True existing documents will persist. Defaults to True. If not
           specified and rows have been previously inserted, will issue a
           warning.
        :param index: list of columns, using +, -, @ as a column prefix to
           specify ASCENDING, DESCENDING, GEOSPHERE respectively. For @ the
           column has to represent a valid GeoJSON object.
        :param timestamp: if True or a field name adds a timestamp. If the
           value is a boolean or datetime, uses _created as the field name.
           The timestamp is always datetime.datetime.utcnow(). May be overriden
           by specifying the tuple (col, datetime).
        :return: the Metadata object created
        """
        from .queryops import MongoQueryOps
        collection = self.collection(name)
        if is_series(obj):
            import pandas as pd
            obj = pd.DataFrame(obj, index=obj.index, columns=[str(obj.name)])
            store_series = True
        else:
            store_series = False
        if append is False:
            self.drop(name, force=True)
        elif append is None and collection.count(limit=1):
            from warnings import warn
            warn('%s already exists, will append rows' % name)
        if index:
            # get index keys
            if isinstance(index, dict):
                idx_kwargs = index
                index = index.pop('columns')
            else:
                idx_kwargs = {}
            # create index with appropriate options
            keys, idx_kwargs = MongoQueryOps().make_index(index, **idx_kwargs)
            collection.create_index(keys, **idx_kwargs)
        if timestamp:
            dt = datetime.utcnow()
            if isinstance(timestamp, bool):
                col = '_created'
            elif isinstance(timestamp, six.string_types):
                col = timestamp
            elif isinstance(timestamp, datetime):
                col, dt = '_created', timestamp
            elif isinstance(timestamp, tuple):
                col, dt = timestamp
            obj[col] = dt
        # store dataframe indicies
        obj, idx_meta = unravel_index(obj)
        stored_columns = [jsonescape(col) for col in obj.columns]
        column_map = list(zip(obj.columns, stored_columns))
        dtypes = {
            dict(column_map).get(k): v.name
            for k, v in iteritems(obj.dtypes)
        }
        kind_meta = {
            'columns': column_map,
            'dtypes': dtypes,
            'idx_meta': idx_meta
        }
        # ensure column names to be strings
        obj.columns = stored_columns
        # create mongon indicies for data frame index columns
        df_idxcols = [col for col in obj.columns if col.startswith('_idx')]
        if df_idxcols:
            keys, idx_kwargs = MongoQueryOps().make_index(df_idxcols)
            collection.create_index(keys, **idx_kwargs)
        # bulk insert
        # -- get native objects
        # -- seems to be required since pymongo 3.3.x. if not converted
        #    pymongo raises Cannot Encode object for int64 types
        obj = obj.astype('O')
        #collection.insert_many((row.to_dict() for i, row in obj.iterrows()))
        # collection.insert_many(obj.to_dict(orient='records'))
        fast_insert(obj, self, name)
        signals.dataset_put.send(sender=None, name=name)
        kind = (Metadata.PANDAS_SEROWS
                if store_series
                else Metadata.PANDAS_DFROWS)
        meta = self._make_metadata(name=name,
                                   prefix=self.prefix,
                                   bucket=self.bucket,
                                   kind=kind,
                                   kind_meta=kind_meta,
                                   attributes=attributes,
                                   collection=collection.name)
        return meta.save()

    def put_dataframe_as_dfgroup(self, obj, name, groupby, attributes=None):
        """ 
        store a dataframe grouped by columns in a mongo document 

        :Example:

          > # each group
          >  {
          >     #group keys
          >     key: val,
          >     _data: [
          >      # only data keys
          >        { key: val, ... }
          >     ]}

        """
        def row_to_doc(obj):
            for gval, gdf in obj.groupby(groupby):
                gval = make_tuple(gval.astype('O'))
                doc = dict(zip(groupby, gval))
                datacols = list(set(gdf.columns) - set(groupby))
                doc['_data'] = gdf[datacols].astype('O').to_dict('records')
                yield doc
        datastore = self.collection(name)
        datastore.drop()
        datastore.insert_many(row_to_doc(obj))
        signals.dataset_put.send(sender=None, name=name)
        return self._make_metadata(name=name,
                                   prefix=self.prefix,
                                   bucket=self.bucket,
                                   kind=Metadata.PANDAS_DFGROUP,
                                   attributes=attributes,
                                   collection=datastore.name).save()

    def put_dataframe_as_hdf(self, obj, name, attributes=None):
        filename = self._get_obj_store_key(name, '.hdf')
        hdffname = self._package_dataframe2hdf(obj, filename)
        with open(hdffname, 'rb') as fhdf:
            fileid = self.fs.put(fhdf, filename=filename)
        signals.dataset_put.send(sender=None, name=name)
        return self._make_metadata(name=name,
                                   prefix=self.prefix,
                                   bucket=self.bucket,
                                   kind=Metadata.PANDAS_HDF,
                                   attributes=attributes,
                                   gridfile=GridFSProxy(grid_id=fileid)).save()

    def put_ndarray_as_hdf(self, obj, name, attributes=None):
        """ store numpy array as hdf

        this is hack, converting the array to a dataframe then storing
        it
        """
        import pandas as pd
        df = pd.DataFrame(obj)
        signals.dataset_put.send(sender=None, name=name)
        return self.put_dataframe_as_hdf(df, name, attributes=attributes)

    def put_pyobj_as_hdf(self, obj, name, attributes=None):
        """
        store list, tuple, dict as hdf

        this requires the list, tuple or dict to be convertible into
        a dataframe
        """
        import pandas as pd
        df = pd.DataFrame(obj)
        signals.dataset_put.send(sender=None, name=name)
        return self.put_dataframe_as_hdf(df, name, attributes=attributes)

    def put_pyobj_as_document(self, obj, name, attributes=None, append=True):
        """
        store a dict as a document

        similar to put_dataframe_as_documents no data will be replaced by
        default. that is, obj is appended as new documents into the objects'
        mongo collection. to replace the data, specify append=False.
        """
        collection = self.collection(name)
        if append is False:
            collection.drop()
        elif append is None and collection.count(limit=1):
            from warnings import warn
            warn('%s already exists, will append rows' % name)
        objid = collection.insert({'data': obj})
        signals.dataset_put.send(sender=None, name=name)
        return self._make_metadata(name=name,
                                   prefix=self.prefix,
                                   bucket=self.bucket,
                                   kind=Metadata.PYTHON_DATA,
                                   collection=collection.name,
                                   attributes=attributes,
                                   objid=objid).save()

    def drop(self, name, force=False, version=-1):
        """
        Drop the object

        :param name: The name of the object
        :param force: If True ignores DoesNotExist exception, defaults to False
            meaning this raises a DoesNotExist exception of the name does not
            exist
        :return:    True if object was deleted, False if not.
                    If force is True and
                    the object does not exist it will still return True
        """
        meta = self.metadata(name, version=version)
        if meta is None and not force:
            raise DoesNotExist()
        collection = self.collection(name)
        if collection:
            self.mongodb.drop_collection(collection.name)
        if meta:
            if meta.collection:
                self.mongodb.drop_collection(meta.collection)
                self._drop_metadata(name)
                return True
            if meta and meta.gridfile is not None:
                meta.gridfile.delete()
                self._drop_metadata(name)
                return True
        return False

    def get_backend_bykind(self, kind, model_store=None, data_store=None,
                           **kwargs):
        """
        return the backend by a given object kind

        :param kind: The object kind
        :param model_store: the OmegaStore instance used to store models
        :param data_store: the OmegaStore instance used to store data
        :param kwargs: the kwargs passed to the backend initialization
        :return: the backend 
        """
        backend_cls = self.defaults.OMEGA_STORE_BACKENDS[kind]
        model_store = model_store or self
        data_store = data_store or self
        backend = backend_cls(model_store=model_store,
                              data_store=data_store, **kwargs)
        return backend

    def get_backend(self, name, model_store=None, data_store=None, **kwargs):
        """
        return the backend by a given object name

        :param kind: The object kind
        :param model_store: the OmegaStore instance used to store models
        :param data_store: the OmegaStore instance used to store data
        :param kwargs: the kwargs passed to the backend initialization
        :return: the backend 
        """
        meta = self.metadata(name)
        if meta is not None:
            backend_cls = self.defaults.OMEGA_STORE_BACKENDS.get(meta.kind)
            if backend_cls:
                model_store = model_store or self
                data_store = data_store or self
                backend = backend_cls(model_store=model_store,
                                      data_store=data_store, **kwargs)
                return backend
        return None

    def getl(self, *args, **kwargs):
        """ return a lazy MDataFrame for a given object

        Same as .get, but returns a MDataFrame

        """
        return self.get(*args, lazy=True, **kwargs)

    def get(self, name, version=-1, force_python=False,
            **kwargs):
        """
        Retrieve an object

        :param name: The name of the object
        :param version: Version of the stored object (not supported)
        :param force_python: Return as a python object
        :param kwargs: kwargs depending on object kind 
        :return: an object, estimator, pipelines, data array or pandas dataframe
            previously stored with put()
        """
        meta = self.metadata(name, version=version)
        if meta is None:
            return None
        if not force_python:
            backend = self.get_backend(name)
            if backend is not None:
                return backend.get(name, **kwargs)
            if meta.kind == Metadata.SKLEARN_JOBLIB:
                backend = self.get_backend(name)
                return backend.get_model(name)
            elif meta.kind == Metadata.SPARK_MLLIB:
                backend = self.get_backend(name)
                return backend.get_model(name, version)
            elif meta.kind == Metadata.PANDAS_DFROWS:
                return self.get_dataframe_documents(name, version=version,
                                                    **kwargs)
            elif meta.kind == Metadata.PANDAS_SEROWS:
                return self.get_dataframe_documents(name, version=version,
                                                    is_series=True,
                                                    **kwargs)
            elif meta.kind == Metadata.PANDAS_DFGROUP:
                return self.get_dataframe_dfgroup(
                    name, version=version, **kwargs)
            elif meta.kind == Metadata.PYTHON_DATA:
                return self.get_python_data(name, version=version)
            elif meta.kind == Metadata.PANDAS_HDF:
                return self.get_dataframe_hdf(name, version=version)
        return self.get_object_as_python(meta, version=version)

    def get_dataframe_documents(self, name, columns=None, lazy=False,
                                filter=None, version=-1, is_series=False,
                                **kwargs):
        """
        Internal method to return DataFrame from documents 

        :param name: the name of the object (str)
        :param columns: the column projection as a list of column names
        :param lazy: if True returns a lazy representation as an MDataFrame. 
           If False retrieves all data and returns a DataFrame (default) 
        :param filter: the filter to be applied as a column__op=value dict 
        :param version: the version to retrieve (not supported)
        :param is_series: if True retruns a Series instead of a DataFrame
        :param kwargs: remaining kwargs are used a filter. The filter kwarg
           overrides other kwargs.
        :return: the retrieved object (DataFrame, Series or MDataFrame)

        """
        collection = self.collection(name)
        if lazy:
            from ..mdataframe import MDataFrame
            filter = filter or kwargs
            df = MDataFrame(collection, columns=columns).query(**filter)
            if is_series:
                df = df[0]
        else:
            # TODO ensure the same processing is applied in MDataFrame
            # TODO this method should always use a MDataFrame disregarding lazy
            filter = filter or kwargs
            if filter:
                from .query import Filter
                query = Filter(collection, **filter).query
                cursor = collection.find(filter=query, projection=columns)
            else:
                cursor = collection.find(projection=columns)
            # restore dataframe
            df = cursor_to_dataframe(cursor)
            if '_id' in df.columns:
                del df['_id']
            meta = self.metadata(name)
            # -- restore columns
            meta_columns = dict(meta.kind_meta.get('columns'))
            if meta_columns:
                # apply projection, if any
                if columns:
                    # get only projected columns
                    # meta_columns is {origin_column: stored_column}
                    orig_columns = dict({k: v for k, v in iteritems(meta_columns)
                                         if k in columns or v in columns})
                else:
                    # restore columns to original name
                    orig_columns = meta_columns
                df.rename(columns=orig_columns, inplace=True)
            # -- restore indexes
            idx_meta = meta.kind_meta.get('idx_meta')
            if idx_meta:
                df = restore_index(df, idx_meta)
            if is_series:
                index = df.index
                name = df.columns[0]
                df = df[name]
                df.index = index
                df.name = None if name == 'None' else name
        signals.dataset_get.send(sender=None, name=name)
        return df

    def rebuild_params(self, kwargs, collection):
        """
        Returns a modified set of parameters for querying mongodb
        based on how the mongo document is structured and the
        fields the document is grouped by.

        **Note: Explicitly to be used with get_grouped_data only**

        :param kwargs: Mongo filter arguments
        :param collection: The name of mongodb collection
        :return: Returns a set of parameters as dictionary.
        """
        modified_params = {}
        db_structure = collection.find_one({}, {'_id': False})
        groupby_columns = list(set(db_structure.keys()) - set(['_data']))
        if kwargs is not None:
            for item in kwargs:
                if item not in groupby_columns:
                    modified_query_param = '_data.' + item
                    modified_params[modified_query_param] = kwargs.get(item)
                else:
                    modified_params[item] = kwargs.get(item)
        return modified_params

    def get_dataframe_dfgroup(self, name, version=-1, kwargs=None):
        """
        Return a grouped dataframe

        :param name: the name of the object
        :param version: not supported
        :param kwargs: mongo db query arguments to be passed to 
               collection.find() as a filter.

        """
        import pandas as pd
        def convert_doc_to_row(cursor):
            for doc in cursor:
                data = doc.pop('_data', [])
                for row in data:
                    doc.update(row)
                    yield doc
        datastore = self.collection(name)
        kwargs = kwargs if kwargs else {}
        params = self.rebuild_params(kwargs, datastore)
        cursor = datastore.find(params, {'_id': False})
        df = pd.DataFrame(convert_doc_to_row(cursor))
        signals.dataset_get.send(sender=None, name=name)
        return df

    def get_dataframe_hdf(self, name, version=-1):
        """
        Retrieve dataframe from hdf

        :param name: The name of object
        :param version: The version of object (not supported)
        :return: Returns a python pandas dataframe
        :raises: gridfs.errors.NoFile
        """
        df = None
        filename = self._get_obj_store_key(name, '.hdf')
        if filename.endswith('.hdf') and self.fs.exists(filename=filename):
            df = self._extract_dataframe_hdf(filename, version=version)
            signals.dataset_get.send(sender=None, name=name)
            return df
        else:
            raise gridfs.errors.NoFile(
                "{0} does not exist in mongo collection '{1}'".format(
                    name, self.bucket))

    def get_python_data(self, name, version=-1):
        """
        Retrieve objects as python data

        :param name: The name of object
        :param version: The version of object

        :return: Returns the object as python list object
        """
        datastore = self.collection(name)
        cursor = datastore.find()
        data = (d.get('data') for d in cursor)
        signals.dataset_get.send(sender=None, name=name)
        return list(data)

    def get_object_as_python(self, meta, version=-1):
        """
        Retrieve object as python object

        :param meta: The metadata object
        :param version: The version of the object

        :return: Returns data as python object
        """
        if meta.kind == Metadata.SKLEARN_JOBLIB:
            return meta.gridfile
        if meta.kind == Metadata.PANDAS_HDF:
            return meta.gridfile
        if meta.kind == Metadata.PANDAS_DFROWS:
            return list(getattr(self.mongodb, meta.collection).find())
        if meta.kind == Metadata.PYTHON_DATA:
            col = getattr(self.mongodb, meta.collection)
            return col.find_one(dict(_id=meta.objid)).get('data')
        raise TypeError('cannot return kind %s as a python object' % meta.kind)

    def list(self, pattern=None, regexp=None, kind=None, raw=False,
             include_temp=False):
        """
        List all files in store

        specify pattern as a unix pattern (e.g. :code:`models/*`,
        or specify regexp)

        :param pattern: the unix file pattern or None for all
        :param regexp: the regexp. takes precedence over pattern
        :param raw: if True return the meta data objects
        :return: List of files in store

        """
        db = self.mongodb
        searchkeys = dict(bucket=self.bucket,
                          prefix=self.prefix)
        if kind or self.force_kind:
            kind = kind or self.force_kind
            if isinstance(kind, (tuple, list)):
                searchkeys.update(kind__in=kind)
            else:
                searchkeys.update(kind=kind)
        meta = Metadata.objects(**searchkeys)
        if raw:
            if regexp:
                files = [f for f in meta if re.match(regexp, f.name)]
            elif pattern:
                files = [f for f in meta if fnmatch(f.name, pattern)]
            else:
                files = [f for f in meta]
        else:
            files = [d.name for d in meta]
            if regexp:
                files = [f for f in files if re.match(regexp, f)]
            elif pattern:
                files = [f for f in files if fnmatch(f, pattern)]
            files = [f.replace('.omm', '') for f in files]
            if not include_temp:
                files = [f for f in files if not f.startswith('_temp')]
        return files

    def object_store_key(self, name, ext):
        """
        Returns the store key

        :param name: The name of object
        :param ext: The extension of the filename

        :return: A filename with relative bucket, prefix and name
        """
        return self._get_obj_store_key(name, ext)

    def _get_obj_store_key(self, name, ext):
        # backwards compatilibity implementation of object_store_key()
        name = '%s.%s' % (name, ext) if not name.endswith(ext) else name
        filename = '{bucket}.{prefix}.{name}'.format(
            bucket=self.bucket,
            prefix=self.prefix,
            name=name,
            ext=ext).replace('/', '_').replace('..', '.')
        return filename

    def _package_dataframe2hdf(self, df, filename, key=None):
        """
        Package a dataframe as a hdf file

        :param df: The dataframe
        :param filename: Name of file

        :return: Filename of hdf file
        """
        lpath = tempfile.mkdtemp()
        fname = os.path.basename(filename)
        hdffname = os.path.join(self.tmppath, fname + '.hdf')
        key = key or 'data'
        df.to_hdf(hdffname, key)
        return hdffname

    def _extract_dataframe_hdf(self, filename, version=-1):
        """
        Extracts a dataframe from a stored hdf file

        :param filename: The name of file
        :param version: The version of file

        :return: Pandas dataframe
        """
        import pandas as pd
        hdffname = os.path.join(self.tmppath, filename)
        dirname = os.path.dirname(hdffname)
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        try:
            outf = self.fs.get_version(filename, version=version)
        except gridfs.errors.NoFile as e:
            raise e
        with open(hdffname, 'wb') as hdff:
            hdff.write(outf.read())
        hdf = pd.HDFStore(hdffname)
        key = list(hdf.keys())[0]
        df = hdf[key]
        hdf.close()
        return df
