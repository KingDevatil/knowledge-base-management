"""
Admin routes — re-exported from routes_pages (GET) and routes_api (POST).

To add a new route:
  - Page (renders HTML): add to routes_pages.py
  - API/form (POST/PUT/DELETE): add to routes_api.py
"""
from .routes_pages import page_router
from .routes_api import api_router

# Merge both routers into a single router for main.py compatibility
from fastapi import APIRouter
router = APIRouter()
router.include_router(page_router)
router.include_router(api_router)
