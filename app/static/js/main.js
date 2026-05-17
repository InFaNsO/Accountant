// Indian comma formatter (shared core): 10000000 → "1,00,00,000"
function _indianCommas(intStr) {
  if (intStr.length <= 3) return intStr;
  const last3 = intStr.slice(-3);
  let rem = intStr.slice(0, -3);
  const groups = [];
  while (rem.length > 0) { groups.unshift(rem.slice(-2)); rem = rem.slice(0, -2); }
  return groups.join(',') + ',' + last3;
}

// Indian Rupee formatter: 10000000 → ₹1,00,00,000.00
function formatINR(value) {
  const num = parseFloat(value) || 0;
  const [intRaw, dec] = Math.abs(num).toFixed(2).split('.');
  return (num < 0 ? '-' : '') + '₹' + _indianCommas(intRaw) + '.' + dec;
}

// Indian quantity formatter (no ₹, no forced decimals): 100000 → "1,00,000"
function formatIndian(value) {
  const num = parseFloat(value);
  if (isNaN(num)) return String(value ?? '0');
  const abs = Math.abs(num);
  const isWhole = abs === Math.floor(abs);
  const intStr = String(Math.floor(abs));
  const dec    = isWhole ? '' : ('.' + abs.toFixed(2).split('.')[1]);
  return (num < 0 ? '-' : '') + _indianCommas(intStr) + dec;
}

// ── Stock unit toggle (pieces ↔ boxes) ──────────────────────
// Call initStockUnit() on DOMContentLoaded on any page with [data-pcs] elements.
// Products have data-ppb (pieces-per-box) on a container; stock numbers have data-pcs.
function setStockUnit(unit) {
  localStorage.setItem('stockUnit', unit);
  _applyStockUnit(unit);
}

function _applyStockUnit(unit) {
  document.querySelectorAll('[data-pcs]').forEach(el => {
    const pcs = parseFloat(el.dataset.pcs);
    if (isNaN(pcs)) return;
    const container = el.closest('[data-ppb]');
    const ppb = container ? (parseFloat(container.dataset.ppb) || 0) : 0;
    el.textContent = (unit === 'boxes' && ppb > 0)
      ? formatIndian(pcs / ppb)
      : formatIndian(pcs);
  });
  document.querySelectorAll('.unit-btn').forEach(btn => {
    btn.classList.toggle('unit-active', btn.dataset.unit === unit);
  });
}

function initStockUnit() {
  const unit = localStorage.getItem('stockUnit') || 'boxes';
  _applyStockUnit(unit);
}

// Shared table search utility
function filterTable(q, tableId) {
  q = q.toLowerCase();
  const table = document.getElementById(tableId);
  if (!table) return;
  table.querySelectorAll('tbody tr').forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// Dark mode — apply saved preference immediately to avoid flash
(function () {
  if (localStorage.getItem('theme') === 'dark') {
    document.body.classList.add('dark');
  }
})();

function toggleDark() {
  const isDark = document.body.classList.toggle('dark');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
}

// Auto-dismiss flash messages after 4s
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.flash').forEach(el => {
    setTimeout(() => el.remove(), 4000);
  });
});
