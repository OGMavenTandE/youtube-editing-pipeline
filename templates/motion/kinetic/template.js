/* kinetic: never two lines on screen at once. Each beat enters ease-out, holds,
   exits over 7 frames ease-in, then 2 empty frames. The last beat holds; hard cut. */
(function () {
  var H = window.YTPH, Y = H.Y, c = H.cues, tl = H.tl;
  H.fill(["label", "source"]);
  var box = H.el("beats");
  var lift = 34 * Y.stage.scale;
  c.beats.forEach(function (beat) {
    var outer = document.createElement("div");
    outer.className = "kn-beat";
    var inner = document.createElement("div");
    inner.id = beat.id;
    outer.appendChild(inner);
    box.appendChild(outer);
    H.text(inner, Y.el[beat.id]);
    tl.fromTo(inner, { opacity: 0, y: lift },
      { opacity: 1, y: 0, duration: c.enter, ease: "power3.out" }, beat["in"]);
    if (beat.out !== null) {
      tl.to(inner, { opacity: 0, y: -lift * 0.6, duration: c.exit, ease: "power2.in" }, beat.out);
    }
  });
  H.show("#label");
  H.show("#source");
  H.finish();
})();
