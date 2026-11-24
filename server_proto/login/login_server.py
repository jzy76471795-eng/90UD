"""Compatibility import for the game startup owner; player HTTPS is owned by Go."""
from login.game_sessions import GameSessionService, LoginApiError
LoginApiServer = GameSessionService
