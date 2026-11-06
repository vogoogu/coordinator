document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]");
  if (b && navigator.clipboard) {
    navigator.clipboard.writeText(b.getAttribute("data-copy"));
    b.textContent = "Copied";
  }
});
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { e.preventDefault(); }
});
