// Runs before the page paints so a saved theme never flashes the wrong colours.
try {
  const saved = localStorage.getItem("theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch { /* storage blocked: follow the system setting */ }
