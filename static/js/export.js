// The prompt: "these slides are ready -- make a Google Slides deck, a
// PowerPoint, or both?" Opened by jobs.js when a video finishes, and by the
// Convert buttons. Several videos finishing together (a playlist) share one
// prompt.
//
// Google Slides is the default. Someone who has not connected Google yet can
// just press Convert: Google's consent screen comes up once, and the export
// finishes on its own when they come back. Until the server has a Google
// OAuth client (Settings), the option is shown but off -- with a button to
// Settings for an admin.
//
// The last choice of formats is remembered in this browser, as a
// convenience; if storage is unavailable the defaults are used.
//
// Converting a video that already has a Google deck replaces that deck (same
// link) unless "Replace" is unticked. That box starts ticked every time.
const Export = {
  me: null,
  pending: [],
  google: { enabled: false , connected: false , email: null },
  storageKey: "videotoslides.export.v2",
  resumeKey: "videotoslides.resumeExport",
  defaults: { pptx: false , gslides: true , pdf: false },

  init( me ) {
    this.me = me;
    const dialog = Dom.get( "export-dialog" );
    Dom.get( "export-form" ).addEventListener( "submit" , this.onSubmit.bind( this ) );
    Dom.get( "export-later" ).addEventListener( "click" , function () { dialog.close(); } );
    Dom.get( "export-review" ).addEventListener( "click" , function () {
      const job = Export.pending[ 0 ];
      dialog.close();
      if ( job ) Review.open( job.id );
    } );
    Dom.get( "export-gslides" ).addEventListener( "change" , this.renderReplace.bind( this ) );
    Dom.get( "export-setup" ).addEventListener( "click" , function () {
      dialog.close();
      App.go( "settings" );
    } );
    dialog.addEventListener( "close" , function () { Export.pending = []; } );
  },

  // From google.js whenever it learns the connection state.
  setGoogle( status ) {
    this.google = Object.assign( this.google , status || {} );
    if ( Dom.get( "export-dialog" ).open ) this.render();
  },

  // Back from Google's consent screen. If the person went there from the
  // prompt, carry out the Google export they asked for and say so; returns
  // whether anything was resumed.
  async resumeAfterGoogle( jobs ) {
    let ids = [];
    try {
      ids = JSON.parse( window.sessionStorage.getItem( this.resumeKey ) || "[]" );
      window.sessionStorage.removeItem( this.resumeKey );
    } catch ( error ) { ids = []; }
    const found = jobs.filter( function ( job ) { return ids.indexOf( job.id ) >= 0 && job.status === "ready"; } );
    if ( !found.length ) return false;
    for ( const job of found ) {
      try {
        await Api.exportJob( job.id , { gslides: true } );
      } catch ( error ) {
        App.showError( job.title + ": " + error.message );
      }
    }
    Jobs.refresh();
    return true;
  },

  // Linked already, or linkable on the spot through the OAuth route.
  googleAvailable() {
    return Boolean( this.google.connected || this.google.enabled );
  },

  remembered() {
    try {
      const saved = JSON.parse( window.localStorage.getItem( this.storageKey ) || "null" );
      if ( saved && typeof saved.pptx === "boolean" && typeof saved.gslides === "boolean" ) {
        return { pptx: saved.pptx , gslides: saved.gslides , pdf: saved.pdf === true };
      }
    } catch ( error ) { /* defaults */ }
    return this.defaults;
  },

  prompt( jobs ) {
    if ( !jobs || !jobs.length ) return;
    const dialog = Dom.get( "export-dialog" );
    const known = this.pending.map( function ( job ) { return job.id; } );
    jobs.forEach( function ( job ) { if ( known.indexOf( job.id ) < 0 ) Export.pending.push( job ); } );
    if ( !dialog.open ) {
      const choice = this.remembered();
      Dom.get( "export-pptx" ).checked = choice.pptx;
      Dom.get( "export-gslides" ).checked = choice.gslides;
      Dom.get( "export-pdf" ).checked = choice.pdf;
      Dom.get( "export-replace" ).checked = true;
    }
    this.render();
    if ( !dialog.open ) dialog.showModal();
  },

  render() {
    const jobs = this.pending;
    const single = jobs.length === 1;
    Dom.text( Dom.get( "export-title" ) , single
      ? I18n.format( "export.ready_one" , { title: jobs[ 0 ].title } )
      : I18n.format( "export.ready_many" , { count: jobs.length } ) );
    const slides = jobs.reduce( function ( sum , job ) { return sum + ( job.included_count || 0 ); } , 0 );
    Dom.text( Dom.get( "export-summary" ) , I18n.format( "export.summary" , { count: slides } ) );

    const list = Dom.get( "export-list" );
    Dom.clear( list );
    if ( !single ) {
      jobs.forEach( function ( job ) {
        list.appendChild( Dom.el( "li" , { text: I18n.format( "export.list_item" , { title: job.title , count: job.included_count } ) } ) );
      } );
    }
    Dom.show( Dom.get( "export-review" ) , single && I18n.get( "export.review_button" ) !== "" );

    const google = this.google;
    const available = this.googleAvailable();
    const gslides = Dom.get( "export-gslides" );
    const pptx = Dom.get( "export-pptx" );
    gslides.disabled = !available;
    if ( !available ) {
      // Shown, but cannot be chosen yet; make sure Convert still does something.
      gslides.checked = false;
      if ( !pptx.checked && !Dom.get( "export-pdf" ).checked ) pptx.checked = true;
    }
    let note = "";
    const admin = Boolean( this.me && this.me.role === "admin" );
    if ( google.connected ) note = I18n.format( "export.google_as" , { email: google.email || "" } );
    else if ( google.enabled ) note = I18n.get( "export.google_will_connect" );
    else note = I18n.get( admin ? "export.google_needs_setup" : "export.google_ask_admin" );
    Dom.text( Dom.get( "export-google-note" ) , note );
    Dom.show( Dom.get( "export-google-note" ) , note !== "" );
    Dom.show( Dom.get( "export-setup" ) , !google.enabled && admin );
    Dom.show( Dom.get( "export-error" ) , false );
    this.renderReplace();
  },

  // Only asked when Google is chosen and some video already has a deck.
  renderReplace() {
    const hasDeck = this.pending.some( function ( job ) { return Boolean( job.gslides_url ); } );
    const gslides = Dom.get( "export-gslides" );
    Dom.show( Dom.get( "export-replace-row" ) , hasDeck && gslides.checked && !gslides.disabled );
  },

  async onSubmit( event ) {
    event.preventDefault();
    const pptx = Dom.get( "export-pptx" ).checked;
    const gslides = Dom.get( "export-gslides" ).checked && this.googleAvailable();
    const pdf = Dom.get( "export-pdf" ).checked;
    const gslidesNew = !Dom.get( "export-replace" ).checked;
    const problem = Dom.get( "export-error" );
    if ( !pptx && !gslides && !pdf ) {
      Dom.text( problem , I18n.get( "export.choose_one" ) );
      Dom.show( problem , true );
      return;
    }
    if ( this.googleAvailable() ) {
      try {
        window.localStorage.setItem( this.storageKey , JSON.stringify( { pptx: pptx , gslides: gslides , pdf: pdf } ) );
      } catch ( error ) { /* not remembered */ }
    }

    // Google chosen but not connected yet: build any PowerPoint / PDF now, then go
    // through Google's consent screen; the Google export runs on return.
    const connectFirst = gslides && !this.google.connected;
    const button = Dom.get( "export-go" );
    button.disabled = true;
    const failures = [];
    for ( const job of this.pending ) {
      if ( connectFirst && !pptx && !pdf ) continue;
      try {
        await Api.exportJob( job.id , { pptx: pptx , pdf: pdf , gslides: connectFirst ? false : gslides , gslides_new: gslidesNew } );
      } catch ( error ) {
        failures.push( job.title + ": " + error.message );
      }
    }
    button.disabled = false;
    if ( failures.length ) {
      Dom.text( problem , failures.join( " — " ) );
      Dom.show( problem , true );
      Jobs.refresh();
      return;
    }
    if ( connectFirst ) {
      try {
        window.sessionStorage.setItem( this.resumeKey , JSON.stringify( this.pending.map( function ( job ) { return job.id; } ) ) );
      } catch ( error ) { /* the Google part then has to be started again by hand */ }
      Google.connect();
      return;
    }
    Dom.get( "export-dialog" ).close();
    Jobs.refresh();
  },
};
