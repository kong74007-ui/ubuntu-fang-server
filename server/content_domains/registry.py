"""Capability registry assembled from domain modules."""

from . import ai_edit, audio, breakdown, image, leads, text, video


HANDLERS = {}
for domain in (image, text, leads, audio, video, breakdown, ai_edit):
    HANDLERS.update(domain.HANDLERS)
