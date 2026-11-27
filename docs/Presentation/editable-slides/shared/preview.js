/* Single-page preview only. The combined deck keeps its existing runtime. */
(() => {
  'use strict';
  const slide = document.querySelector('#deck > .slide');
  const stage = document.querySelector('#stage');
  const deck = document.querySelector('#deck');
  slide.setAttribute('aria-label', `${Number(slide.dataset.editablePage)}번 장표. ${slide.dataset.title}`);
  function fit() {
    const workspace = document.querySelector('#workspace');
    const style = getComputedStyle(workspace);
    const width = workspace.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = workspace.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const scale = Math.max(.12, Math.min(width / 1600, height / 900));
    stage.style.width = `${1600 * scale}px`;
    stage.style.height = `${900 * scale}px`;
    deck.style.transform = `scale(${scale})`;
  }
  function notes() {
    const panel = document.querySelector('#notes-panel');
    const open = panel.hidden;
    panel.hidden = !open;
    document.body.classList.toggle('body-notes', open);
    document.querySelector('#notes-button').setAttribute('aria-expanded', String(open));
    fit();
  }
  // The slide contains visible content only. Notes live in the adjacent notes.html.
  // An iframe works for file:// previews without requiring fetch or a web server.
  document.querySelector('#notes-button').addEventListener('click', notes);
  document.querySelector('#notes-close').addEventListener('click', notes);
  document.querySelector('#print-page').addEventListener('click', async () => {
    await Promise.all([...document.images].map(image => image.decode().catch(() => {})));
    window.print();
  });
  slide.addEventListener('click', event => {
    const source = event.target.closest('[data-business-details]');
    if (source) window.open(`../../presentation.pdf#page=${source.dataset.businessDetails.split(',')[0]}`, '_blank', 'noopener');
  });
  document.addEventListener('keydown', event => {
    if (event.altKey || event.metaKey || event.ctrlKey || event.target.closest('input,textarea,select,[contenteditable]')) return;
    const key = event.key.toLowerCase();
    if (key === 'n') { event.preventDefault(); notes(); }
    if (key === 'escape' && !document.querySelector('#notes-panel').hidden) notes();
    const link = document.querySelector(key === 'arrowright' ? '[data-next-page]' : key === 'arrowleft' ? '[data-previous-page]' : '.no-link');
    if (link) { event.preventDefault(); location.href = link.href; }
  });
  slide.hidden = false;
  addEventListener('resize', fit);
  document.fonts.ready.then(fit);
  fit();
})();
