/* count-up: before bar counts up in slate; the after row reveals only once the
   before number lands, then counts up in amber. Holds; hard cut. */
(function () {
  var H = window.YTPH, Y = H.Y, c = H.cues, d = H.data, tl = H.tl;
  H.fill(["label", "title", "before_label", "after_label", "before_num", "after_num", "tag", "source"]);
  var format = function (v) { return H.fmt(v, d.decimals) + (d.unit ? " " + d.unit : ""); };
  d.rows.forEach(function (r) {
    var row = H.el("row-" + r.key);
    row.querySelector(".cu-track").style.height = r.bar_h + "px";
    var num = H.el(r.key + "_num");
    num.style.left = r.num_x + "px";
    num.classList.add(r.inside ? "in" : "out");
  });
  var b = d.rows[0], a = d.rows[1];
  H.inUp("#label", c.label, 0.3, 12);
  H.inUp("#title", c.title, 0.6, 24);
  H.inUp("#row-before", c.before_row, 0.4, 16);
  H.inUp("#source", c.source, 0.4, 0);
  tl.fromTo("#row-before .cu-fill", { width: "0%" },
    { width: b.pct + "%", duration: c.before_count[1] - c.before_count[0], ease: "power2.out" },
    c.before_count[0]);
  H.count(H.el("before_num"), 0, b.value, c.before_count[0], c.before_count[1], format);
  H.inUp("#row-after", c.after_row, 0.3, 16);
  tl.fromTo("#row-after .cu-fill", { width: "0%" },
    { width: a.pct + "%", duration: c.after_count[1] - c.after_count[0], ease: "power2.out" },
    c.after_count[0]);
  H.count(H.el("after_num"), 0, a.value, c.after_count[0], c.after_count[1], format);
  H.inUp("#tag", c.tag, 0.35, 14);
  H.finish();
})();
