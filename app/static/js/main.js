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
