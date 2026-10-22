// Light by default, always. Dark only when someone picks it with the header
// button, and then remembered in this browser. prefers-color-scheme is
// deliberately ignored, so the app looks the same for everyone until they
// choose otherwise.
//
// The first half runs immediately from <head>, before the page paints, so a
// saved dark choice never flashes light first. Storage can be unavailable
// (private windows, blocked site data); the page is then simply light.
( function () {
  try {
    if ( window.localStorage.getItem( "videotoslides.theme" ) === "dark" ) {
      document.documentElement.setAttribute( "data-theme" , "dark" );
    }
  } catch ( error ) { /* light it is */ }
} )();

const Theme = {
  key: "videotoslides.theme",

  isDark() {
    return document.documentElement.getAttribute( "data-theme" ) === "dark";
  },

  // Called once language.yaml has loaded, since the button's label is text.
  init() {
    const button = document.getElementById( "theme-toggle" );
    if ( !button ) return;
    const label = function () {
      button.textContent = I18n.get( Theme.isDark() ? "theme.use_light" : "theme.use_dark" );
      button.hidden = button.textContent === "";
    };
    button.addEventListener( "click" , function () {
      const dark = !Theme.isDark();
      if ( dark ) document.documentElement.setAttribute( "data-theme" , "dark" );
      else document.documentElement.removeAttribute( "data-theme" );
      try { window.localStorage.setItem( Theme.key , dark ? "dark" : "light" ); } catch ( error ) { /* not remembered */ }
      label();
    } );
    label();
  },
};
