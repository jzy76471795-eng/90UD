"""Local server control plane.

The control API deliberately owns transport, authentication, and audit only.
Game-domain mutations remain on the owning Game connection thread.
"""
