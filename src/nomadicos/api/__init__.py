"""NomadicOS local service layer (ADR-0032).

HTTP on 127.0.0.1 wrapping the existing Runtime in-process. Goals execute in
background tasks; state is tracked in an in-memory registry; every endpoint
requires the bearer token from data/api-token (I12: never logged).
"""
