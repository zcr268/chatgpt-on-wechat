/* Console version badge, filled from the backend.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

/* =====================================================================
   CowAgent Console - Main Application Script
   ===================================================================== */

// =====================================================================
// Version — fetched from backend (single source: /VERSION file)
// =====================================================================
let APP_VERSION = '';

