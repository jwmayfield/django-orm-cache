from django.db.models.manager import Manager
from django.db.models.base import ModelBase, Model
from django.core import validators
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from django.db.models.fields import AutoField, ImageField, FieldDoesNotExist
from django.db.models.fields.related import OneToOneRel, ManyToOneRel
from django.db.models.query import delete_objects
from django.db.models.options import Options, AdminOptions
from django.db import connection, transaction
from django.db.models import signals
from django.db.models.loading import register_models, get_model
from django.dispatch import dispatcher
from django.utils.datastructures import SortedDict
from django.utils.functional import curry
from django.utils.encoding import smart_str, force_unicode, smart_unicode
from django.conf import settings
from itertools import izip
import types
import sys
import os

from django.db import models
from django.core.cache import cache

from manager import CacheManager
from utils import get_cache_key_for_pk

DEFAULT_CACHE_TIME = 60*60*60 # the maximum an item should be in the cache

class CachedModelBase(ModelBase):
    def __new__(cls, name, bases, attrs):
        # If this isn't a subclass of CachedModel, don't do anything special.
        try:
            if not filter(lambda b: issubclass(b, CachedModel), bases):
                return super(CachedModelBase, cls).__new__(cls, name, bases, attrs)
        except NameError:
            # 'CachedModel' isn't defined yet, meaning we're looking at Django's own
            # Model class, defined below.
            return super(CachedModelBase, cls).__new__(cls, name, bases, attrs)

        # Create the class.
        new_class = type.__new__(cls, name, bases, {'__module__': attrs.pop('__module__')})
        new_class.add_to_class('_meta', Options(attrs.pop('Meta', None)))
        new_class.add_to_class('DoesNotExist', types.ClassType('DoesNotExist', (ObjectDoesNotExist,), {}))
        new_class.add_to_class('MultipleObjectsReturned',
            types.ClassType('MultipleObjectsReturned', (MultipleObjectsReturned, ), {}))

        # Build complete list of parents
        for base in bases:
            # TODO: Checking for the presence of '_meta' is hackish.
            if '_meta' in dir(base):
                new_class._meta.parents.append(base)
                new_class._meta.parents.extend(base._meta.parents)


        if getattr(new_class._meta, 'app_label', None) is None:
            # Figure out the app_label by looking one level up.
            # For 'django.contrib.sites.models', this would be 'sites'.
            model_module = sys.modules[new_class.__module__]
            new_class._meta.app_label = model_module.__name__.split('.')[-2]

        # Bail out early if we have already created this class.
        m = get_model(new_class._meta.app_label, name, False)
        if m is not None:
            return m

        # Add all attributes to the class.
        for obj_name, obj in attrs.items():
            new_class.add_to_class(obj_name, obj)

        # Add Fields inherited from parents
        for parent in new_class._meta.parents:
            for field in parent._meta.fields:
                # Only add parent fields if they aren't defined for this class.
                try:
                    new_class._meta.get_field(field.name)
                except FieldDoesNotExist:
                    field.contribute_to_class(new_class, field.name)

        new_class._prepare()

        register_models(new_class._meta.app_label, new_class)
        # Because of the way imports happen (recursively), we may or may not be
        # the first class for this model to register with the framework. There
        # should only be one class for each model, so we must always return the
        # registered version.
        return get_model(new_class._meta.app_label, name, False)

class CachedModel(Model):
    """
    docstring for CachedModel
    """
    __metaclass__ = CachedModelBase

#    objects = CacheManager()
#    nocache = Manager()
    
    # Maybe this would work?
    @classmethod
    def _prepare(cls):
        # How do we extend the parent classes classmethod properly?
        # super(CachedModel, cls)._prepare() errors
        opts = cls._meta
        opts._prepare(cls)

        if opts.order_with_respect_to:
            cls.get_next_in_order = curry(cls._get_next_or_previous_in_order, is_next=True)
            cls.get_previous_in_order = curry(cls._get_next_or_previous_in_order, is_next=False)
            setattr(opts.order_with_respect_to.rel.to, 'get_%s_order' % cls.__name__.lower(), curry(method_get_order, cls))
            setattr(opts.order_with_respect_to.rel.to, 'set_%s_order' % cls.__name__.lower(), curry(method_set_order, cls))

        # Give the class a docstring -- its definition.
        if cls.__doc__ is None:
            cls.__doc__ = "%s(%s)" % (cls.__name__, ", ".join([f.attname for f in opts.fields]))

        if hasattr(cls, 'get_absolute_url'):
            cls.get_absolute_url = curry(get_absolute_url, opts, cls.get_absolute_url)

        cls.add_to_class('objects', CacheManager())
        cls.add_to_class('nocache', Manager())
        cls.add_to_class('_default_manager', cls.nocache)
        dispatcher.send(signal=signals.class_prepared, sender=cls)
    
    @staticmethod
    def _get_cache_key_for_pk(model, pk):
        return get_cache_key_for_pk(model, pk)
    
    @property
    def cache_key(self):
        return self._get_cache_key_for_pk(self.__class__, self.pk)
    
    def save(self, *args, **kwargs):
        cache.set(self._get_cache_key_for_pk(self.__class__, self.pk), self)
        super(CachedModel, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # TODO: create an option that tells the model whether or not it should
        # do a cache.delete when the object is deleted. For memcached we
        # wouldn't care about deleting.
        cache.delete(self._get_cache_key_for_pk(self.__class__, self.pk))
        super(CachedModel, self).delete(*args, **kwargs)