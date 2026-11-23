"use strict";
// Applies the saved theme before the page is drawn: "light", "dark" or "system" (no attribute).
(() => {
  try {
    const theme = localStorage.getItem("nipovpn.theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  } catch (e) { /* storage unavailable: follow the system */ }
})();
