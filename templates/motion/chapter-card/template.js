/* chapter-card: label, amber rule draws on, title words rise in a stagger. Holds; hard cut. */
(function () {
  var H = window.YTPH, c = H.cues, tl = H.tl, Y = H.Y;
  H.fill(["label", "title", "subline", "source"]);
  H.inUp("#label", c.label, 0.3, 10);
  tl.fromTo(".cc-rule", { scaleX: 0 }, { scaleX: 1, duration: 0.45, ease: "power3.out" }, c.rule);
  tl.fromTo("#title .w", { opacity: 0, y: 30 * Y.stage.scale },
    { opacity: 1, y: 0, duration: 0.5, ease: "power3.out", stagger: c.stagger }, c.title);
  H.inUp("#subline", c.subline, 0.4, 14);
  H.inUp("#source", c.source, 0.4, 0);
  H.finish();
})();
