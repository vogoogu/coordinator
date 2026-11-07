/* Small helpers for the board. Every page works without them; they add copy
   buttons, confirmations, the modal New task dialog, the note hint on the
   state form and folding columns on narrow screens. */
(function () {
  var wide = window.matchMedia("(min-width: 641px)");

  function openDialog(dlg) {
    if (!dlg || !dlg.showModal) { return false; }
    if (!dlg.open) { dlg.showModal(); }
    var first = dlg.querySelector("input:not([type=hidden]):not([type=radio]), textarea");
    if (first) { first.focus(); }
    return true;
  }

  document.addEventListener("click", function (e) {
    var copy = e.target.closest("[data-copy]");
    if (copy && navigator.clipboard) {
      navigator.clipboard.writeText(copy.getAttribute("data-copy"));
      var label = copy.querySelector(".cl");
      if (label) { label.textContent = "Copied"; }
      return;
    }
    var opener = e.target.closest("[data-dialog]");
    if (opener && openDialog(document.getElementById(opener.getAttribute("data-dialog")))) {
      e.preventDefault();
      return;
    }
    var closer = e.target.closest("[data-close]");
    if (closer) {
      var dlg = closer.closest("dialog");
      if (dlg && dlg.open) { e.preventDefault(); dlg.close(); }
      return;
    }
    // On wide screens columns are always open: their headers do not fold.
    if (wide.matches && e.target.closest(".col summary")) { e.preventDefault(); }
  });

  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  });

  function foldColumns() {
    var cols = document.querySelectorAll(".col details");
    for (var i = 0; i < cols.length; i++) {
      cols[i].open = wide.matches || cols[i].getAttribute("data-fold") !== "closed";
    }
  }

  function noteHint(select) {
    var note = document.getElementById(select.getAttribute("data-note-for"));
    var opt = select.options[select.selectedIndex];
    if (!note || !opt) { return; }
    var needed = opt.getAttribute("data-note") === "1";
    note.required = needed;
    var mark = note.form && note.form.querySelector("[data-note-req]");
    if (mark) { mark.hidden = !needed; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    foldColumns();
    if (wide.addEventListener) { wide.addEventListener("change", foldColumns); }
    var selects = document.querySelectorAll("[data-note-for]");
    for (var i = 0; i < selects.length; i++) {
      noteHint(selects[i]);
      selects[i].addEventListener("change", function () { noteHint(this); });
    }
    if (location.hash === "#new-task") { openDialog(document.getElementById("new-task")); }
  });
})();
