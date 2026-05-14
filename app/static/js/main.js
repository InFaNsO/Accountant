// Indian Rupee formatter: 10000000 → ₹1,00,00,000.00
function formatINR(value) {
  const num = parseFloat(value) || 0;
  const [intRaw, dec] = Math.abs(num).toFixed(2).split('.');
  let formatted;
  if (intRaw.length <= 3) {
    formatted = intRaw;
  } else {
    const last3 = intRaw.slice(-3);
    let rem = intRaw.slice(0, -3);
    const groups = [];
    while (rem.length > 0) { groups.unshift(rem.slice(-2)); rem = rem.slice(0, -2); }
    formatted = groups.join(',') + ',' + last3;
  }
  return (num < 0 ? '-' : '') + '₹' + formatted + '.' + dec;
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
