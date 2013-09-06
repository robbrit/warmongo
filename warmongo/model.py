# Copyright 2013 Rob Britton
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from datetime import datetime
import database

import inflect
import re

from exceptions import ValidationError, InvalidSchemaException

from bson import ObjectId
from copy import deepcopy

inflect_engine = inflect.engine()

ValidTypes = {
    "integer": int,
    "boolean": bool,
    "number": float,
    "string": basestring,
    "object_id": ObjectId,
    "date": datetime
}


class Model(object):
    def __init__(self, fields={}, from_find=False, *args, **kwargs):
        ''' Creates an instance of the object.'''
        self._from_find = from_find

        fields = deepcopy(fields)

        # populate any default fields for objects that haven't come from the DB
        if not from_find:
            for field, details in self._schema["properties"].items():
                if "default" in details and not field in fields:
                    fields[field] = details["default"]

        self._fields = fields
        self.validate()

    def reload(self):
        ''' Reload this object's data from the DB. '''
        self._fields = self.__class__.find_by_id(self._id)._fields

    def save(self):
        ''' Saves an object to the database. '''
        self.validate()
        self._id = self.collection().save(self._fields)

    def delete(self):
        ''' Removes an object from the database. '''
        if self._id:
            self.collection().remove({"_id": ObjectId(str(self._id))})

    def get(self, field, default=None):
        ''' Get a field if it exists, otherwise return the default. '''
        return self._fields.get(field, default)

    @classmethod
    def find_or_create(cls, query, *args, **kwargs):
        ''' Retrieve an element from the database. If it doesn't exist, create
        it.  Calling this method is equivalent to calling find_one and then
        creating an object. Note that this method is not atomic.  '''
        result = cls.find_one(query, *args, **kwargs)

        if result is None:
            default = cls._schema.get("default", {})
            default.update(query)

            result = cls(default, *args, **kwargs)

        return result

    @classmethod
    def find(cls, *args, **kwargs):
        ''' Grabs a set of elements from the DB.
        Note: This returns a generator, so you can't to do an efficient count.
        To get a count, use the count() function which accepts the same
        arguments as find() with the exception of non-query fields like sort,
        limit, skip.
        '''
        options = {}

        for option in ["sort", "limit", "skip"]:
            if option in kwargs:
                options[option] = kwargs[option]
                del options[option]

        result = cls.collection().find(*args, **kwargs)

        if "sort" in options:
            result = result.sort(options["sort"])

        if "skip" in options:
            result = result.skip(options["skip"])

        if "limit" in options:
            result = result.limit(options["limit"])

        for obj in result:
            yield cls(obj, from_find=True)

    @classmethod
    def find_by_id(cls, id, **kwargs):
        ''' Finds a single object from this collection. '''
        if isinstance(id, basestring):
            id = ObjectId(id)

        args = {"_id": id}

        result = cls.collection().find_one(args, **kwargs)
        if result is not None:
            return cls(result, from_find=True)
        return None

    @classmethod
    def find_one(cls, *args, **kwargs):
        ''' Finds a single object from this collection. '''
        result = cls.collection().find_one(*args, **kwargs)
        if result is not None:
            return cls(result)
        return None

    @classmethod
    def count(cls, *args, **kwargs):
        ''' Counts the number of items:
            - not the same as pymongo's count, this is the equivalent to:
                collection.find(*args, **kwargs).count()
        '''
        return cls.collection().find(*args, **kwargs).count()

    @classmethod
    def collection(cls):
        ''' Get the pymongo collection object for this model. Useful for
        features not supported by Warmongo like aggregate queries and
        map-reduce. '''
        return database.get_collection(collection=cls.collection_name(),
                                       database=cls.database_name())

    @classmethod
    def collection_name(cls):
        ''' Get the collection associated with this class. The convention is
        to take the lowercase of the class name and pluralize it. '''
        global inflect_engine
        if cls._schema.get("collectionName"):
            return cls._schema.get("collectionName")
        elif cls._schema.get("name"):
            name = cls._schema.get("name")
        else:
            name = cls.__name__

        # convert to snake case
        name = (name[0] + re.sub('([A-Z])', r'_\1', name[1:])).lower()

        # pluralize
        return inflect_engine.plural(name)

    @classmethod
    def database_name(cls):
        ''' Get the database associated with this class. Meant to be overridden
        in subclasses. '''
        if cls._schema.get("databaseName"):
            return cls._schema.get("databaseName")
        return None

    def validate(self):
        ''' Validate `schema` against a dict `obj`. '''
        self.validate_field("", self._schema, self._fields)

    def validate_field(self, key, value_schema, value):
        ''' Validate a single field in `value` named `key` against `value_schema`. '''
        # check the type
        value_type = value_schema.get("type", "object")

        if value_type == "array":
            self.validate_array(key, value_schema, value)
        elif value_type == "object":
            self.validate_object(key, value_schema, value)
        else:
            self.validate_simple(key, value_schema, value)

    def validate_array(self, key, value_schema, value):
        if not isinstance(value, list):
            raise ValidationError("Field '%s' is of type 'array', received '%s' (%s)" %
                                  (key, str(value), type(value)))

        if value_schema.get("items"):
            for item in value:
                self.validate_field(key, value_schema["items"], item)
        else:
            # no items, this is an untyped array
            pass

    def validate_object(self, key, value_schema, value):
        if not isinstance(value, dict):
            raise ValidationError("Field '%s' is of type 'object', received '%s' (%s)" %
                                  (key, str(value), type(value)))

        if not value_schema.get("properties"):
            # no validation on this object
            return

        for subkey, subvalue in value_schema["properties"].items():
            if subkey in value:
                self.validate_field(subkey, subvalue, value[subkey])
            elif subvalue.get("required", False) and not self._from_find:
                # if the field is required and we haven't pulled from find,
                # throw an exception
                raise ValidationError("Field '%s' is required but not found!" %
                                        subkey)

        # Check for additional properties
        if not value_schema.get("additionalProperties", True):
            extra = set(value.keys()) - set(value_schema["properties"].keys())

            if len(extra) > 0:
                raise ValidationError("Additional properties are not allowed: %s" %
                                        ', '.join(list(extra)))

    def validate_simple(self, key, value_schema, value):
        ''' Validate a simple field (not an object or array) against a schema. '''
        value_type = value_schema.get("type", "any")

        if value_type == "any":
            # can be anything
            pass
        elif value_type == "number":
            # special case: can be an int or a float
            if not isinstance(value, int) and not isinstance(value, float):
                raise ValidationError("Field '%s' is of type '%s', received '%s' (%s)" %
                                      (key, value_type, str(value), type(value)))
        elif value_type in ValidTypes:
            if not isinstance(value, ValidTypes[value_type]):
                raise ValidationError("Field '%s' is of type '%s', received '%s' (%s)" %
                                      (key, value_type, str(value), type(value)))
            # TODO: check other things like maximum, etc.
        else:
            # unknown type
            raise InvalidSchemaException("Unknown type '%s'!" % value_type)

    def __getattr__(self, attr):
        ''' Get an attribute from the fields we've selected. Note that if the
        field doesn't exist, this will return None. '''
        if attr in self._schema["properties"] and attr in self._fields:
            return self._fields.get(attr)
        else:
            raise AttributeError("%s has no attribute '%s'" % (str(self), attr))

    def __setattr__(self, attr, value):
        ''' Set one of the fields, with validation. Exception is on "private"
        fields - the ones that start with _. '''
        if attr.startswith("_"):
            return object.__setattr__(self, attr, value)

        if attr in self._schema["properties"]:
            # Check the field against our schema
            self.validate_field(attr, self._schema["properties"][attr], value)
        elif not self._schema.get("additionalProperties", True):
            # not allowed to add additional properties
            raise ValidationError("Additional property '%s' not allowed!" % attr)

        self._fields[attr] = value
        return value
