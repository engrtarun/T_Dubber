/* T9 Cloud brand layer — injected by scripts/brand-t9.mjs */
(function () {
  var BRAND = "T9 Cloud";

  // Dark first, before anything paints, so there is no white flash.
  function dark() {
    document.documentElement.setAttribute("data-theme", "fluent-dark");
    document.body && document.body.setAttribute("data-theme", "fluent-dark");
  }
  dark();
  document.addEventListener("DOMContentLoaded", dark);

  function rebrand() {
    document.title = document.title.replace(/OneDrive/g, BRAND);

    // Text nodes only — never touch attributes, or links break.
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    nodes.forEach(function (node) {
      if (node.nodeValue && node.nodeValue.indexOf("OneDrive") !== -1) {
        node.nodeValue = node.nodeValue.replace(/OneDrive/g, BRAND);
      }
    });

    // Storage upsells: gone at the DOM level too, not just hidden by CSS.
    // The left-nav purchase button is matched by id AND class — the capture
    // gives it no data-automationid, which is why a selector written for the
    // banner alone silently misses it.
    document
      .querySelectorAll(
        ".od-QuotaBannerWrapper, .od-QBannerUpsell-main, .od-QBannerUpsell-barBtn, " +
          "#LeftNavStorageButton, .buyStorageButton_7ff103ac, " +
          ".getMoreStorageButton_7ff103ac, " +
          '[data-automationid="getMoreStorageButton"], ' +
          '[data-automationid="upgradeButton"]'
      )
      .forEach(function (el) { el.remove(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", rebrand);
  } else {
    rebrand();
  }

  // The capture boots a React tree that can re-render these nodes after load.
  new MutationObserver(rebrand).observe(document.documentElement, {
    childList: true, subtree: true,
  });
})();
