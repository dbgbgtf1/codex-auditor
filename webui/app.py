#!/usr/bin/env python3
"""Compatibility entrypoint for Gunicorn and local development."""

from __future__ import annotations

from auditor_webui.web import create_app, create_wsgi_app, main

__all__ = ["create_app", "create_wsgi_app", "main"]


if __name__ == "__main__":
    main()
