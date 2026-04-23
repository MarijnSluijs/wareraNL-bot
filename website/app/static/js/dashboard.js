(async () => {
  if (window.location.pathname !== "/") return;
  const refresh = async () => {
    try {
      const res = await fetch("/api/dashboard/overview?days=7", { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      const cards = document.querySelectorAll(".kpi");
      const map = data.kpis || {};
      const keys = Object.keys(map);
      cards.forEach((card, index) => {
        const key = keys[index];
        if (!key) return;
        const value = map[key].value;
        const valueNode = card.querySelector(".value");
        if (valueNode) valueNode.textContent = value;
      });
    } catch (_) {
      // no-op refresh failure
    }
  };
  setInterval(refresh, 30000);
})();
