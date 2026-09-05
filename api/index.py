"""Vercel WSGI entry point for CivicVoice."""
from app import create_app

app = create_app()

