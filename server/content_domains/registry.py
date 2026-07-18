"""Capability registry assembled from domain modules."""

from . import audio, breakdown, image, leads, text, video, ai_edit


HANDLERS = {}
for domain in (image, text, leads, audio, video, breakdown, ai_edit):
    HANDLERS.update(domain.HANDLERS)
