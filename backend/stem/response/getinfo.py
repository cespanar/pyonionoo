import stem.socket
import stem.response

class GetInfoResponse(stem.response.ControlMessage):
  """
  Reply for a GETINFO query.
  
  :var dict entries: mapping between the queried options and their values
  """
  
  def _parse_message(self):
    # Example:
    # 250-version=0.2.3.11-alpha-dev (git-ef0bc7f8f26a917c)
    # 250+config-text=
    # ControlPort 9051
    # DataDirectory /home/atagar/.tor
    # ExitPolicy reject *:*
    # Log notice stdout
    # Nickname Unnamed
    # ORPort 9050
    # .
    # 250 OK
    
    self.entries = {}
    remaining_lines = list(self)
    
    if not self.is_ok() or not remaining_lines.pop() == "OK":
      unrecognized_keywords = []
      for code, _, line in self.content():
        if code == '552' and line.startswith("Unrecognized key \"") and line.endswith("\""):
          unrecognized_keywords.append(line[18:-1])
      
      if unrecognized_keywords:
        raise stem.socket.InvalidArguments("552", "GETINFO request contained unrecognized keywords: %s\n" \
            % ', '.join(unrecognized_keywords), unrecognized_keywords)
      else:
        raise stem.socket.ProtocolError("GETINFO response didn't have an OK status:\n%s" % self)
    
    while remaining_lines:
      try:
        key, value = remaining_lines.pop(0).split("=", 1)
      except ValueError:
        raise stem.socket.ProtocolError("GETINFO replies should only contain parameter=value mappings:\n%s" % self)
      
      # if the value is a multiline value then it *must* be of the form
      # '<key>=\n<value>'
      
      if "\n" in value:
        if not value.startswith("\n"):
          raise stem.socket.ProtocolError("GETINFO response contained a multiline value that didn't start with a newline:\n%s" % self)
        
        value = value[1:]
      
      self.entries[key] = value

