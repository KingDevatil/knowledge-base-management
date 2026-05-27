"""
Admin routes — merged from all sub-route files for main.py compatibility.

To add a new route:
  - Page (renders HTML): add to routes_pages.py
  - API/form (POST/PUT/DELETE): add to routes_documents_api.py, routes_admin_misc.py,
    routes_shares_api.py, or routes_api.py (login/account)
"""
from .routes_pages import page_router
from .routes_api import api_router
from .routes_documents_api import documents_router
from .routes_admin_misc import admin_misc_router
from .routes_shares_api import shares_router

from fastapi import APIRouter

router = APIRouter()
router.include_router(page_router)
router.include_router(api_router)
router.include_router(documents_router)
router.include_router(admin_misc_router)
router.include_router(shares_router)
