/* receipt-image: Studio's finished receipt (a PNG) prints in from a fixed top edge.
   The slot and the first lip of paper are on screen from frame 1; the paper feeds down
   at a steady speed for `print` seconds, then holds; hard cut. The image is drawn as
   is: the viewport only reveals it, never recolors, crops or rescales it beyond `scale`. */
(function () {
  var H = window.YTPH, c = H.cues, d = H.data, tl = H.tl;
  var slot = H.el("ri-slot"), vp = H.el("ri-viewport"), img = H.el("ri-paper");
  slot.style.left = d.slot_x + "px";
  slot.style.top = d.slot_y + "px";
  slot.style.width = d.slot_w + "px";
  slot.style.height = d.slot_h + "px";
  vp.style.left = d.x + "px";
  vp.style.top = d.y + "px";
  vp.style.width = d.w + "px";
  vp.style.height = d.lip + "px";
  img.style.width = d.w + "px";
  img.style.height = d.h + "px";
  tl.set(vp, { height: d.lip }, 0);
  tl.to(vp, { height: d.h, duration: c.print, ease: "none" }, 0);
  H.finish();
})();
