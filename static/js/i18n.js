// Pulls language.yaml (as JSON) from the server and stamps it into the page.
//
// The contract with language.yaml is what makes this more than a translation
// layer: a key that is missing or empty means "do not show this element". So
// removing a label, a button, or an entire section is an edit to the YAML,
// not to the markup.
//
// Note the asymmetry in apply(): a missing key hides its element, but a
// present key never un-hides one. Plenty of elements (error banners, the
// admin panel) are hidden by app.js based on state, and i18n has no business
// overruling that -- it decides what text says, not what the app reveals.
const I18n = {
  data: {},

  async load() {
    const response = await fetch( "/api/language" , { credentials: "same-origin" } );
    if ( !response.ok ) throw new Error( "could not load language file" );
    this.data = await response.json();
    return this.data;
  },

  // Resolve a dotted key like "admin.create_button".
  get( key ) {
    let cursor = this.data;
    const parts = String( key ).split( "." );
    for ( let index = 0; index < parts.length; index += 1 ) {
      if ( cursor == null || typeof cursor !== "object" ) return "";
      cursor = cursor[ parts[ index ] ];
    }
    return typeof cursor === "string" ? cursor : "";
  },

  // get() with {placeholders} filled in: format( "jobs.slides" , { count: 3 } ).
  // Values are inserted as text and end up in textContent, never as markup.
  format( key , values ) {
    return this.get( key ).replace( /\{(\w+)\}/g , function ( match , name ) {
      return values && values[ name ] != null ? String( values[ name ] ) : match;
    } );
  },

  // 1.2 GB, 340 MB -- the unit names are language.yaml text too.
  bytes( count ) {
    const units = [ "units.bytes" , "units.kb" , "units.mb" , "units.gb" , "units.tb" ];
    let value = Number( count ) || 0;
    let index = 0;
    while ( value >= 1024 && index < units.length - 1 ) { value /= 1024; index += 1; }
    const shown = index === 0 ? String( value ) : value.toFixed( value < 10 ? 1 : 0 );
    return this.format( units[ index ] , { value: shown } );
  },

  // 1:02:03 or 4:05 -- a video timestamp, so digits and colons only.
  clock( seconds ) {
    const total = Math.max( 0 , Math.round( Number( seconds ) || 0 ) );
    const h = Math.floor( total / 3600 );
    const m = Math.floor( ( total % 3600 ) / 60 );
    const s = total % 60;
    const pad = function ( n ) { return n < 10 ? "0" + n : String( n ); };
    return h > 0 ? h + ":" + pad( m ) + ":" + pad( s ) : m + ":" + pad( s );
  },

  apply( root ) {
    const scope = root || document;

    Dom.all( "[data-i18n]" , scope ).forEach( function ( element ) {
      const value = I18n.get( element.getAttribute( "data-i18n" ) );
      if ( value === "" ) {
        element.hidden = true;
        return;
      }
      if ( element.tagName === "TITLE" ) {
        document.title = value;
        return;
      }
      element.textContent = value;
    } );

    // data-i18n-attr="placeholder:account.name_placeholder,title:some.key"
    Dom.all( "[data-i18n-attr]" , scope ).forEach( function ( element ) {
      element.getAttribute( "data-i18n-attr" ).split( "," ).forEach( function ( pair ) {
        const halves = pair.split( ":" );
        if ( halves.length !== 2 ) return;
        const value = I18n.get( halves[ 1 ].trim() );
        if ( value !== "" ) element.setAttribute( halves[ 0 ].trim() , value );
      } );
    } );
  },
};
