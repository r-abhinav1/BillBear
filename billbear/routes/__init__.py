"""The app's pages and endpoints, one blueprint per area."""
from billbear.routes import cron, host, join, pages, picks, results, rooms, scan

BLUEPRINTS = (pages.bp, scan.bp, rooms.bp, join.bp, picks.bp, host.bp, results.bp, cron.bp)
