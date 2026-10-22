// Tiny DOM helpers, shared by every page. Kept in its own file so a new page
// can use them without loading the account/admin logic in app.js.
//
// The reason this exists at all rather than each page reaching for innerHTML:
// building elements through createElement + textContent means a display name
// containing "<script>" is rendered as those literal characters. Assembling
// the same row as an HTML string would execute it. There is no escaping
// helper here on purpose -- the safe path should be the only path.
const Dom = {
  get( id ) {
    return document.getElementById( id );
  },

  all( selector , root ) {
    return Array.from( ( root || document ).querySelectorAll( selector ) );
  },

  show( element , visible ) {
    if ( element ) element.hidden = !visible;
  },

  text( element , value ) {
    if ( element ) element.textContent = value == null ? "" : String( value );
  },

  clear( element ) {
    if ( element ) element.replaceChildren();
  },

  // el( "button", { class: "small", text: name, on: { click: fn } } )
  el( tag , options ) {
    const settings = options || {};
    const node = document.createElement( tag );
    if ( settings.class ) node.className = settings.class;
    if ( settings.text != null ) node.textContent = String( settings.text );
    if ( settings.attrs ) {
      Object.entries( settings.attrs ).forEach( function ( entry ) {
        node.setAttribute( entry[ 0 ] , entry[ 1 ] );
      } );
    }
    if ( settings.on ) {
      Object.entries( settings.on ).forEach( function ( entry ) {
        node.addEventListener( entry[ 0 ] , entry[ 1 ] );
      } );
    }
    ( settings.children || [] ).forEach( function ( child ) {
      node.appendChild( child );
    } );
    return node;
  },

  // Copy an element's text, with a selection fallback. The fallback is not
  // optional politeness: navigator.clipboard needs a secure context, so every
  // plain-http://localhost run lands there, and a copy button that silently
  // does nothing is worse than no button. Used for both login links and API
  // keys, which are exactly the strings nobody wants to retype by hand.
  async copy( source , notice ) {
    const value = source ? source.textContent : "";
    try {
      await navigator.clipboard.writeText( value );
      this.flash( notice );
    } catch ( error ) {
      const range = document.createRange();
      range.selectNodeContents( source );
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange( range );
    }
  },

  // Briefly reveal a confirmation ("Saved.", "Copied.") without leaving it on
  // screen forever.
  flash( element , milliseconds ) {
    if ( !element ) return;
    element.hidden = false;
    window.clearTimeout( element._flashTimer );
    element._flashTimer = window.setTimeout( function () {
      element.hidden = true;
    } , milliseconds || 2000 );
  },
};
