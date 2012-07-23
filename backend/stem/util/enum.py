"""
Basic enumeration, providing ordered types for collections. These can be
constructed as simple type listings, ie:

::

  >>> insects = Enum("ANT", "WASP", "LADYBUG", "FIREFLY")
  >>> insects.ANT
  'Ant'
  >>> tuple(insects)
  ('Ant', 'Wasp', 'Ladybug', 'Firefly')

with overwritten string counterparts:

::

  >>> pets = Enum(("DOG", "Skippy"), "CAT", ("FISH", "Nemo"))
  >>> pets.DOG
  'Skippy'
  >>> pets.CAT
  'Cat'

**Module Overview:**

::

  to_camel_case - converts a string to camel case
  UppercaseEnum - Provides an enum instance with capitalized values.
  Enum - Provides a basic, ordered  enumeration.
    |- keys - string representation of our enum keys
    |- index_of - indice of an enum value
    |- next - provides the enum after a given enum value
    |- previous - provides the enum before a given value
    |- __getitem__ - provides the value for an enum key
    +- __iter__ - iterator over our enum keys
"""

def to_camel_case(label, word_divider = " "):
  """
  Converts the given string to camel case, ie:
  
  ::
  
    >>> to_camel_case("I_LIKE_PEPPERJACK!")
    'I Like Pepperjack!'
  
  :param str label: input string to be converted
  :param str word_divider: string used to replace underscores
  """
  
  words = []
  for entry in label.split("_"):
    if len(entry) == 0: words.append("")
    elif len(entry) == 1: words.append(entry.upper())
    else: words.append(entry[0].upper() + entry[1:].lower())
  
  return word_divider.join(words)

def UppercaseEnum(*args):
  """
  Provides an Enum instance where the values are identical to the keys. Since
  the keys are uppercase by convention this means the values are too. For
  instance...
  
  ::
  
    >>> runlevels = UppercaseEnum("DEBUG", "INFO", "NOTICE", "WARN", "ERROR")
    >>> runlevels.DEBUG
    'DEBUG'
  
  :param list args: enum keys to initialize with
  
  :returns: :class:`stem.util.Enum` instance with the given keys
  """
  
  return Enum(*[(v, v) for v in args])

class Enum(object):
  """
  Basic enumeration.
  """
  
  def __init__(self, *args):
    # ordered listings of our keys and values
    keys, values = [], []
    
    for entry in args:
      if isinstance(entry, str):
        key, val = entry, to_camel_case(entry)
      elif isinstance(entry, tuple) and len(entry) == 2:
        key, val = entry
      else: raise ValueError("Unrecognized input: %s" % args)
      
      keys.append(key)
      values.append(val)
      self.__dict__[key] = val
    
    self._keys = tuple(keys)
    self._values = tuple(values)
  
  def keys(self):
    """
    Provides an ordered listing of the enumeration keys in this set.
    
    :returns: tuple with our enum keys
    """
    
    return self._keys
  
  def index_of(self, value):
    """
    Provides the index of the given value in the collection.
    
    :param str value: entry to be looked up
    
    :returns: integer index of the given entry
    
    :raises: ValueError if no such element exists
    """
    
    return self._values.index(value)
  
  def next(self, value):
    """
    Provides the next enumeration after the given value.
    
    :param str value: enumeration for which to get the next entry
    
    :returns: enum value following the given entry
    
    :raises: ValueError if no such element exists
    """
    
    if not value in self._values:
      raise ValueError("No such enumeration exists: %s (options: %s)" % (value, ", ".join(self._values)))
    
    # TODO: python 2.5 lacks an index method on tuples, when we drop support
    # we can drop this hack
    next_index = (list(self._values).index(value) + 1) % len(self._values)
    return self._values[next_index]
  
  def previous(self, value):
    """
    Provides the previous enumeration before the given value.
    
    :param str value: enumeration for which to get the previous entry
    
    :returns: enum value proceeding the given entry
    
    :raises: ValueError if no such element exists
    """
    
    if not value in self._values:
      raise ValueError("No such enumeration exists: %s (options: %s)" % (value, ", ".join(self._values)))
    
    # TODO: python 2.5 lacks an index method on tuples, when we drop support
    # we can drop this hack
    prev_index = (list(self._values).index(value) - 1) % len(self._values)
    return self._values[prev_index]
  
  def __getitem__(self, item):
    """
    Provides the values for the given key.
    
    :param str item: key to be looked up
    
    :returns: str with the value for the given key
    
    :raises: ValueError if the key doesn't exist
    """
    
    if item in self.__dict__:
      return self.__dict__[item]
    else:
      keys = ", ".join(self.keys())
      raise ValueError("'%s' isn't among our enumeration keys, which includes: %s" % (item, keys))
  
  def __iter__(self):
    """
    Provides an ordered listing of the enums in this set.
    """
    
    for entry in self._values:
      yield entry

