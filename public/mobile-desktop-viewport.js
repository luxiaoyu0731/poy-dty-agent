/** Phones use the same 1440 × 900 canvas as a desktop, with native page zoom. */
(() => {
  if (!window.matchMedia("(pointer: coarse)").matches || Math.min(screen.width, screen.height) > 767) return;

  const viewport = document.querySelector('meta[name="viewport"]');
  if (!viewport) return;
  const frameWidth = 1440;
  const frameHeight = 900;
  document.documentElement.classList.add("mobile-desktop-view");
  let lastWidth = 0;
  let lastHeight = 0;

  const fit = () => {
    // Window dimensions are independent of the virtual layout and pinch zoom.
    const width = window.outerWidth || screen.width;
    const height = window.outerHeight || screen.height;
    if (Math.abs(width - lastWidth) < 2 && Math.abs(height - lastHeight) < 2) return;
    // Browser toolbars change height during scrolling; only refit on width changes.
    if (lastWidth && Math.abs(width - lastWidth) < 2) return;
    lastWidth = width;
    lastHeight = height;
    const scale = Math.min(width / frameWidth, height / frameHeight);
    viewport.content = `width=${frameWidth}, initial-scale=${scale}, user-scalable=yes`;
  };
  fit();
  window.addEventListener("resize", fit);
})();
