"""
Classes for interacting with the tor control socket.

Controllers are a wrapper around a ControlSocket, retaining many of its methods
(connect, close, is_alive, etc) in addition to providing its own for
interacting at a higher level.

**Module Overview:**

::

  from_port - Provides a Controller based on a port connection.
  from_socket_file - Provides a Controller based on a socket file connection.
  
  Controller - General controller class intended for direct use.
    |- get_info - issues a GETINFO query for a parameter
    |- get_conf - gets the value of a configuration option
    |- get_conf_mapping - gets the values of multiple configuration options
    |- set_conf - sets the value of a configuration option
    |- reset_conf - reverts configuration options to their default values
    |- set_options - sets or resets the values of multiple configuration options
    |- load_conf - loads configuration information as if it was in the torrc
    |- save_conf - saves configuration information to the torrc
    |- get_version - convenience method to get tor version
    |- authenticate - convenience method to authenticate the controller
    +- protocolinfo - convenience method to get the protocol info
  
  BaseController - Base controller class asynchronous message handling.
    |- msg - communicates with the tor process
    |- is_alive - reports if our connection to tor is open or closed
    |- connect - connects or reconnects to tor
    |- close - shuts down our connection to the tor process
    |- get_socket - provides the socket used for control communication
    |- add_status_listener - notifies a callback of changes in our status
    |- remove_status_listener - prevents further notification of status changes
    +- __enter__ / __exit__ - manages socket connection
"""

from __future__ import with_statement

import time
import Queue
import threading

import stem.connection
import stem.response
import stem.socket
import stem.version
import stem.util.log as log

# state changes a control socket can have
# INIT   - new control connection
# RESET  - received a reset/sighup signal
# CLOSED - control connection closed

State = stem.util.enum.Enum("INIT", "RESET", "CLOSED")

# Constant to indicate an undefined argument default. Usually we'd use None for
# this, but users will commonly provide None as the argument so need something
# else fairly unique...

UNDEFINED = "<Undefined_ >"

# Configuration options that are fetched by a special key. The keys are
# lowercase to make case insensetive lookups easier.

MAPPED_CONFIG_KEYS = {
  "hiddenservicedir": "HiddenServiceOptions",
  "hiddenserviceport": "HiddenServiceOptions",
  "hiddenserviceversion": "HiddenServiceOptions",
  "hiddenserviceauthorizeclient": "HiddenServiceOptions",
  "hiddenserviceoptions": "HiddenServiceOptions"
}

# TODO: The Thread's isAlive() method and theading's currentThread() was
# changed to the more conventional is_alive() and current_thread() in python
# 2.6 and above. We should use that when dropping python 2.5 compatability.

class BaseController(object):
  """
  Controller for the tor process. This is a minimal base class for other
  controllers, providing basic process communication and event listing. Don't
  use this directly - subclasses like the Controller provide higher level
  functionality.
  
  Do not continue to directly interacte with the ControlSocket we're
  constructed from - use our wrapper methods instead.
  """
  
  def __init__(self, control_socket):
    self._socket = control_socket
    self._msg_lock = threading.RLock()
    
    self._status_listeners = [] # tuples of the form (callback, spawn_thread)
    self._status_listeners_lock = threading.RLock()
    
    # queues where incoming messages are directed
    self._reply_queue = Queue.Queue()
    self._event_queue = Queue.Queue()
    
    # thread to continually pull from the control socket
    self._reader_thread = None
    
    # thread to pull from the _event_queue and call handle_event
    self._event_notice = threading.Event()
    self._event_thread = None
    
    # saves our socket's prior _connect() and _close() methods so they can be
    # called along with ours
    
    self._socket_connect = self._socket._connect
    self._socket_close = self._socket._close
    
    self._socket._connect = self._connect
    self._socket._close = self._close
    
    if self._socket.is_alive():
      self._launch_threads()
  
  def msg(self, message):
    """
    Sends a message to our control socket and provides back its reply.
    
    :param str message: message to be formatted and sent to tor
    
    :returns: :class:`stem.response.ControlMessage` with the response
    
    :raises:
      * :class:`stem.socket.ProtocolError` the content from the socket is malformed
      * :class:`stem.socket.SocketError` if a problem arises in using the socket
      * :class:`stem.socket.SocketClosed` if the socket is shut down
    """
    
    with self._msg_lock:
      # If our _reply_queue isn't empty then one of a few things happened...
      #
      # - Our connection was closed and probably re-restablished. This was
      #   in reply to pulling for an asynchronous event and getting this is
      #   expected - ignore it.
      #
      # - Pulling for asynchronous events produced an error. If this was a
      #   ProtocolError then it's a tor bug, and if a non-closure SocketError
      #   then it was probably a socket glitch. Deserves an INFO level log
      #   message.
      #
      # - This is a leftover response for a msg() call. We can't tell who an
      #   exception was airmarked for, so we only know that this was the case
      #   if it's a ControlMessage. This should not be possable and indicates
      #   a stem bug. This deserves a NOTICE level log message since it
      #   indicates that one of our callers didn't get their reply.
      
      while not self._reply_queue.empty():
        try:
          response = self._reply_queue.get_nowait()
          
          if isinstance(response, stem.socket.SocketClosed):
            pass # this is fine
          elif isinstance(response, stem.socket.ProtocolError):
            log.info("Tor provided a malformed message (%s)" % response)
          elif isinstance(response, stem.socket.ControllerError):
            log.info("Socket experienced a problem (%s)" % response)
          elif isinstance(response, stem.response.ControlMessage):
            log.notice("BUG: the msg() function failed to deliver a response: %s" % response)
        except Queue.Empty:
          # the empty() method is documented to not be fully reliable so this
          # isn't entirely surprising
          
          break
      
      try:
        self._socket.send(message)
        response = self._reply_queue.get()
        
        # If the message we received back had an exception then re-raise it to the
        # caller. Otherwise return the response.
        
        if isinstance(response, stem.socket.ControllerError):
          raise response
        else:
          return response
      except stem.socket.SocketClosed, exc:
        # If the recv() thread caused the SocketClosed then we could still be
        # in the process of closing. Calling close() here so that we can
        # provide an assurance to the caller that when we raise a SocketClosed
        # exception we are shut down afterward for realz.
        
        self.close()
        raise exc
  
  def is_alive(self):
    """
    Checks if our socket is currently connected. This is a passthrough for our
    socket's is_alive() method.
    
    :returns: bool that's True if we're shut down and False otherwise
    """
    
    return self._socket.is_alive()
  
  def connect(self):
    """
    Reconnects our control socket. This is a passthrough for our socket's
    connect() method.
    
    :raises: :class:`stem.socket.SocketError` if unable to make a socket
    """
    
    self._socket.connect()
  
  def close(self):
    """
    Closes our socket connection. This is a passthrough for our socket's
    :func:`stem.socket.ControlSocket.close` method.
    """
    
    self._socket.close()
  
  def get_socket(self):
    """
    Provides the socket used to speak with the tor process. Communicating with
    the socket directly isn't advised since it may confuse the controller.
    
    :returns: :class:`stem.socket.ControlSocket` we're communicating with
    """
    
    return self._socket
  
  def add_status_listener(self, callback, spawn = True):
    """
    Notifies a given function when the state of our socket changes. Functions
    are expected to be of the form...
    
    ::
    
      my_function(controller, state, timestamp)
    
    The state is a value from stem.socket.State, functions **must** allow for
    new values in this field. The timestamp is a float for the unix time when
    the change occured.
    
    This class only provides ``State.INIT`` and ``State.CLOSED`` notifications.
    Subclasses may provide others.
    
    If spawn is True then the callback is notified via a new daemon thread. If
    false then the notice is under our locks, within the thread where the
    change occured. In general this isn't advised, especially if your callback
    could block for a while.
    
    :param function callback: function to be notified when our state changes
    :param bool spawn: calls function via a new thread if True, otherwise it's part of the connect/close method call
    """
    
    with self._status_listeners_lock:
      self._status_listeners.append((callback, spawn))
  
  def remove_status_listener(self, callback):
    """
    Stops listener from being notified of further events.
    
    :param function callback: function to be removed from our listeners
    
    :returns: bool that's True if we removed one or more occurances of the callback, False otherwise
    """
    
    with self._status_listeners_lock:
      new_listeners, is_changed = [], False
      
      for listener, spawn in self._status_listeners:
        if listener != callback:
          new_listeners.append((listener, spawn))
        else: is_changed = True
      
      self._status_listeners = new_listeners
      return is_changed
  
  def __enter__(self):
    return self
  
  def __exit__(self, exit_type, value, traceback):
    self.close()
  
  def _handle_event(self, event_message):
    """
    Callback to be overwritten by subclasses for event listening. This is
    notified whenever we receive an event from the control socket.
    
    :param stem.response.ControlMessage event_message: message received from the control socket
    """
    
    pass
  
  def _connect(self):
    self._launch_threads()
    self._notify_status_listeners(State.INIT, True)
    self._socket_connect()
  
  def _close(self):
    # Our is_alive() state is now false. Our reader thread should already be
    # awake from recv() raising a closure exception. Wake up the event thread
    # too so it can end.
    
    self._event_notice.set()
    
    # joins on our threads if it's safe to do so
    
    for t in (self._reader_thread, self._event_thread):
      if t and t.isAlive() and threading.currentThread() != t:
        t.join()
    
    self._notify_status_listeners(State.CLOSED, False)
    self._socket_close()
  
  def _notify_status_listeners(self, state, expect_alive = None):
    """
    Informs our status listeners that a state change occured.
    
    States imply that our socket is either alive or not, which may not hold
    true when multiple events occure in quick succession. For instance, a
    sighup could cause two events (``State.RESET`` for the sighup and
    ``State.CLOSE`` if it causes tor to crash). However, there's no guarentee
    of the order in which they occure, and it would be bad if listeners got the
    ``State.RESET`` last, implying that we were alive.
    
    If set, the expect_alive flag will discard our event if it conflicts with
    our current :func:`stem.control.BaseController.is_alive` state.
    
    :param stem.socket.State state: state change that has occured
    :param bool expect_alive: discard event if it conflicts with our :func:`stem.control.BaseController.is_alive` state
    """
    
    # Any changes to our is_alive() state happen under the send lock, so we
    # need to have it to ensure it doesn't change beneath us.
    
    # TODO: when we drop python 2.5 compatability we can simplify this
    with self._socket._get_send_lock():
      with self._status_listeners_lock:
        change_timestamp = time.time()
        
        if expect_alive != None and expect_alive != self.is_alive():
          return
        
        for listener, spawn in self._status_listeners:
          if spawn:
            name = "%s notification" % state
            args = (self, state, change_timestamp)
            
            notice_thread = threading.Thread(target = listener, args = args, name = name)
            notice_thread.setDaemon(True)
            notice_thread.start()
          else:
            listener(self, state, change_timestamp)
  
  def _launch_threads(self):
    """
    Initializes daemon threads. Threads can't be reused so we need to recreate
    them if we're restarted.
    """
    
    # In theory concurrent calls could result in multple start() calls on a
    # single thread, which would cause an unexpeceted exception. Best be safe.
    
    with self._socket._get_send_lock():
      if not self._reader_thread or not self._reader_thread.isAlive():
        self._reader_thread = threading.Thread(target = self._reader_loop, name = "Tor Listener")
        self._reader_thread.setDaemon(True)
        self._reader_thread.start()
      
      if not self._event_thread or not self._event_thread.isAlive():
        self._event_thread = threading.Thread(target = self._event_loop, name = "Event Notifier")
        self._event_thread.setDaemon(True)
        self._event_thread.start()
  
  def _reader_loop(self):
    """
    Continually pulls from the control socket, directing the messages into
    queues based on their type. Controller messages come in two varieties...
    
    * Responses to messages we've sent (GETINFO, SETCONF, etc).
    * Asynchronous events, identified by a status code of 650.
    """
    
    while self.is_alive():
      try:
        control_message = self._socket.recv()
        
        if control_message.content()[-1][0] == "650":
          # asynchronous message, adds to the event queue and wakes up its handler
          self._event_queue.put(control_message)
          self._event_notice.set()
        else:
          # response to a msg() call
          self._reply_queue.put(control_message)
      except stem.socket.ControllerError, exc:
        # Assume that all exceptions belong to the reader. This isn't always
        # true, but the msg() call can do a better job of sorting it out.
        #
        # Be aware that the msg() method relies on this to unblock callers.
        
        self._reply_queue.put(exc)
  
  def _event_loop(self):
    """
    Continually pulls messages from the _event_queue and sends them to our
    handle_event callback. This is done via its own thread so subclasses with a
    lengthy handle_event implementation don't block further reading from the
    socket.
    """
    
    while True:
      try:
        event_message = self._event_queue.get_nowait()
        self._handle_event(event_message)
      except Queue.Empty:
        if not self.is_alive(): break
        
        self._event_notice.wait()
        self._event_notice.clear()

class Controller(BaseController):
  """
  Communicates with a control socket. This is built on top of the
  BaseController and provides a more user friendly API for library users.
  """
  
  def from_port(control_addr = "127.0.0.1", control_port = 9051):
    """
    Constructs a ControlPort based Controller.
    
    :param str control_addr: ip address of the controller
    :param int control_port: port number of the controller
    
    :returns: :class:`stem.control.Controller` attached to the given port
    
    :raises: :class:`stem.socket.SocketError` if we're unable to establish a connection
    """
    
    control_port = stem.socket.ControlPort(control_addr, control_port)
    return Controller(control_port)
  
  def from_socket_file(socket_path = "/var/run/tor/control"):
    """
    Constructs a ControlSocketFile based Controller.
    
    :param str socket_path: path where the control socket is located
    
    :returns: :class:`stem.control.Controller` attached to the given socket file
    
    :raises: :class:`stem.socket.SocketError` if we're unable to establish a connection
    """
    
    control_socket = stem.socket.ControlSocketFile(socket_path)
    return Controller(control_socket)
  
  from_port = staticmethod(from_port)
  from_socket_file = staticmethod(from_socket_file)
  
  def get_info(self, param, default = UNDEFINED):
    """
    Queries the control socket for the given GETINFO option. If provided a
    default then that's returned if the GETINFO option is undefined or the
    call fails for any reason (error response, control port closed, initiated,
    etc).
    
    :param str,list param: GETINFO option or options to be queried
    :param object default: response if the query fails
    
    :returns:
      Response depends upon how we were called as follows...
      
      * str with the response if our param was a str
      * dict with the param => response mapping if our param was a list
      * default if one was provided and our call failed
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails and we weren't provided a default response
      :class:`stem.socket.InvalidArguments` if the 'param' requested was invalid
    """
    
    # TODO: add caching?
    # TODO: special geoip handling?
    # TODO: add logging, including call runtime
    
    if isinstance(param, str):
      is_multiple = False
      param = [param]
    else:
      is_multiple = True
    
    try:
      response = self.msg("GETINFO %s" % " ".join(param))
      stem.response.convert("GETINFO", response)
      
      # error if we got back different parameters than we requested
      requested_params = set(param)
      reply_params = set(response.entries.keys())
      
      if requested_params != reply_params:
        requested_label = ", ".join(requested_params)
        reply_label = ", ".join(reply_params)
        
        raise stem.socket.ProtocolError("GETINFO reply doesn't match the parameters that we requested. Queried '%s' but got '%s'." % (requested_label, reply_label))
      
      if is_multiple:
        return response.entries
      else:
        return response.entries[param[0]]
    except stem.socket.ControllerError, exc:
      if default == UNDEFINED: raise exc
      else: return default
  
  def get_version(self):
    """
    A convenience method to get tor version that current controller is
    connected to.
    
    :returns: :class:`stem.version.Version`
    
    :raises:
      * :class:`stem.socket.ControllerError` if unable to query the version
      * ValueError if unable to parse the version
    """
    
    return stem.version.Version(self.get_info("version"))
  
  def authenticate(self, *args, **kwargs):
    """
    A convenience method to authenticate the controller.
    
    :param: see :func:`stem.connection.authenticate`
    
    :raises: see :func:`stem.connection.authenticate`
    """
    
    stem.connection.authenticate(self, *args, **kwargs)
  
  def protocolinfo(self):
    """
    A convenience method to get the protocol info of the controller.
    
    :returns: :class:`stem.response.protocolinfo.ProtocolInfoResponse` provided by tor
    
    :raises:
      * :class:`stem.socket.ProtocolError` if the PROTOCOLINFO response is malformed
      * :class:`stem.socket.SocketError` if problems arise in establishing or using the socket
    """
    
    return stem.connection.get_protocolinfo(self)
  
  def get_conf(self, param, default = UNDEFINED, multiple = False):
    """
    Queries the control socket for the value of a given configuration option. If
    provided a default then that's returned as if the GETCONF option is undefined
    or if the call fails for any reason (invalid configuration option, error
    response, control port closed, initiated, etc). If the configuration key
    consists of whitespace only, None is returned unless a default value is given.
    
    :param str param: configuration option to be queried
    :param object default: response if the query fails
    :param bool multiple: if True, the value(s) provided are lists of all returned values, otherwise this just provides the first value
    
    :returns:
      Response depends upon how we were called as follows...
      
      * str with the response if multiple was False
      * list with the response strings if multiple was True
      * default if one was provided and our call failed
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails and we weren't provided a default response
      :class:`stem.socket.InvalidArguments` if the configuration option requested was invalid
    """
    
    # Config options are case insensitive and don't contain whitespace. Using
    # strip so the following check will catch whitespace-only params.
    
    param = param.lower().strip()
    
    if not param:
      return default if default != UNDEFINED else None
    
    entries = self.get_conf_map(param, default, multiple)
    return _case_insensitive_lookup(entries, param, default)
  
  def get_conf_map(self, param, default = UNDEFINED, multiple = True):
    """
    Queries the control socket for the values of given configuration options
    and provides a mapping of the keys to the values. If provided a default
    then that's returned if the GETCONF option is undefined or if the call
    fails for any reason (invalid configuration option, error response, control
    port closed, initiated, etc). Configuration keys that are empty or contain
    only whitespace are ignored.
    
    There's three use cases for GETCONF:
      1. a single value is provided
      2. multiple values are provided for the option queried
      3. a set of options that weren't necessarily requested are returned (for instance querying HiddenServiceOptions gives HiddenServiceDir, HiddenServicePort, etc)
    
    The vast majority of the options fall into the first two categories, in
    which case calling get_conf() is sufficient. However, for batch queries or
    the special options that give a set of values this provides back the full
    response. As of tor version 0.2.1.25 HiddenServiceOptions was the only
    option like this.
    
    The get_conf() and get_conf_map() functions both try to account for these
    special mappings, so queried like get_conf("HiddenServicePort") should
    behave as you'd expect. This method, however, simply returns whatever Tor
    provides so get_conf_map("HiddenServicePort") will give the same response
    as get_conf_map("HiddenServiceOptions").
    
    :param str,list param: configuration option(s) to be queried
    :param object default: response if the query fails
    :param bool multiple: if True, the value(s) provided are lists of all returned values,otherwise this just provides the first value
    
    :returns:
      Response depends upon how we were called as follows...
      
      * dict of 'config key => value' mappings, the value is a list if 'multiple' is True and a str of just the first value otherwise
      * default if one was provided and our call failed
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails and we weren't provided a default response
      :class:`stem.socket.InvalidArguments` if the configuration option requested was invalid
    """
    
    if isinstance(param, str):
      param = [param]
    
    try:
      # remove strings which contain only whitespace
      param = filter(lambda entry: entry.strip(), param)
      if param == []: return {}
      
      # translate context sensitive options
      lookup_param = set([MAPPED_CONFIG_KEYS.get(entry, entry) for entry in param])
      
      response = self.msg("GETCONF %s" % ' '.join(lookup_param))
      stem.response.convert("GETCONF", response)
      
      # Maps the entries back to the parameters that the user requested so the
      # capitalization matches (ie, if they request "exitpolicy" then that
      # should be the key rather than "ExitPolicy"). When the same
      # configuration key is provided multiple times this determines the case
      # based on the first and ignores the rest.
      #
      # This retains the tor provided camel casing of MAPPED_CONFIG_KEYS
      # entries since the user didn't request those by their key, so we can't
      # be sure what they wanted.
      
      for key in response.entries:
        if not key.lower() in MAPPED_CONFIG_KEYS.values():
          user_expected_key = _case_insensitive_lookup(param, key, key)
          
          if key != user_expected_key:
            response.entries[user_expected_key] = response.entries[key]
            del response.entries[key]
      
      if multiple:
        return response.entries
      else:
        return dict([(entry[0], entry[1][0]) for entry in response.entries.items()])
    except stem.socket.ControllerError, exc:
      if default != UNDEFINED: return default
      else: raise exc
  
  def set_conf(self, param, value):
    """
    Changes the value of a tor configuration option. Our value can be any of
    the following...
    
    * a string to set a single value
    * a list of strings to set a series of values (for instance the ExitPolicy)
    * None to either set the value to 0/NULL
    
    :param str param: configuration option to be set
    :param str,list value: value to set the parameter to
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails
      :class:`stem.socket.InvalidArguments` if configuration options requested was invalid
      :class:`stem.socket.InvalidRequest` if the configuration setting is impossible or if there's a syntax error in the configuration values
    """
    
    self.set_options({param: value}, False)
  
  def reset_conf(self, *params):
    """
    Reverts one or more parameters to their default values.
    
    :param str params: configuration option to be reset
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails
      :class:`stem.socket.InvalidArguments` if configuration options requested was invalid
      :class:`stem.socket.InvalidRequest` if the configuration setting is impossible or if there's a syntax error in the configuration values
    """
    
    self.set_options(dict([(entry, None) for entry in params]), True)
  
  def set_options(self, params, reset = False):
    """
    Changes multiple tor configuration options via either a SETCONF or
    RESETCONF query. Both behave identically unless our value is None, in which
    case SETCONF sets the value to 0 or NULL, and RESETCONF returns it to its
    default value. This accepts str, list, or None values in a similar fashion
    to :func:`stem.control.Controller.set_conf`. For example...
    
    ::
    
      my_controller.set_options({
        "Nickname", "caerSidi",
        "ExitPolicy": ["accept *:80", "accept *:443", "reject *:*"],
        "ContactInfo": "caerSidi-exit@someplace.com",
        "Log": None,
      })
    
    The params can optionally be a list a key/value tuples, though the only
    reason this type of arguement would be useful is for hidden service
    configuration (those options are order dependent).
    
    :param dict,list params: mapping of configuration options to the values we're setting it to
    :param bool reset: issues a RESETCONF, returning None values to their defaults if True
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails
      :class:`stem.socket.InvalidArguments` if configuration options requested was invalid
      :class:`stem.socket.InvalidRequest` if the configuration setting is impossible or if there's a syntax error in the configuration values
    """
    
    # constructs the SETCONF or RESETCONF query
    query_comp = ["RESETCONF" if reset else "SETCONF"]
    
    if isinstance(params, dict):
      params = params.items()
    
    for param, value in params:
      if isinstance(value, str):
        query_comp.append("%s=\"%s\"" % (param, value.strip()))
      elif value:
        query_comp.extend(["%s=\"%s\"" % (param, val.strip()) for val in value])
      else:
        query_comp.append(param)
    
    response = self.msg(" ".join(query_comp))
    stem.response.convert("SINGLELINE", response)
    
    if not response.is_ok():
      if response.code == "552":
        if response.message.startswith("Unrecognized option: Unknown option '"):
          key = response.message[37:response.message.find("\'", 37)]
          raise stem.socket.InvalidArguments(response.code, response.message, [key])
        raise stem.socket.InvalidRequest(response.code, response.message)
      elif response.code in ("513", "553"):
        raise stem.socket.InvalidRequest(response.code, response.message)
      else:
        raise stem.socket.ProtocolError("Returned unexpected status code: %s" % response.code)
  
  def load_conf(self, configtext):
    """
    Sends the configuration text to Tor and loads it as if it has been read from
    the torrc.
    
    :param str configtext: the configuration text
    
    :raises: :class:`stem.socket.ControllerError` if the call fails
    """
    
    response = self.msg("LOADCONF\n%s" % configtext)
    stem.response.convert("SINGLELINE", response)
    
    if response.code in ("552", "553"):
      if response.code == "552" and response.message.startswith("Invalid config file: Failed to parse/validate config: Unknown option"):
        raise stem.socket.InvalidArguments(response.code, response.message, [response.message[70:response.message.find('.', 70) - 1]])
      raise stem.socket.InvalidRequest(response.code, response.message)
    elif not response.is_ok():
      raise stem.socket.ProtocolError("+LOADCONF Received unexpected response\n%s" % str(response))
  
  def save_conf(self):
    """
    Saves the current configuration options into the active torrc file.
    
    :raises:
      :class:`stem.socket.ControllerError` if the call fails
      :class:`stem.socket.OperationFailed` if the client is unable to save the configuration file
    """
    
    response = self.msg("SAVECONF")
    stem.response.convert("SINGLELINE", response)
    
    if response.is_ok():
      return True
    elif response.code == "551":
      raise stem.socket.OperationFailed(response.code, response.message)
    else:
      raise stem.socket.ProtocolError("SAVECONF returned unexpected response code")

def _case_insensitive_lookup(entries, key, default = UNDEFINED):
  """
  Makes a case insensitive lookup within a list or dictionary, providing the
  first matching entry that we come across.
  
  :param list,dict entries: list or dictionary to be searched
  :param str key: entry or key value to look up
  :param object default: value to be returned if the key doesn't exist
  
  :returns: case insensitive match or default if one was provided and key wasn't found
  
  :raises: ValueError if no such value exists
  """
  
  if isinstance(entries, dict):
    for k, v in entries.items():
      if k.lower() == key.lower():
        return v
  else:
    for entry in entries:
      if entry.lower() == key.lower():
        return entry
  
  if default != UNDEFINED: return default
  else: raise ValueError("key '%s' doesn't exist in dict: %s" % (key, entries))

