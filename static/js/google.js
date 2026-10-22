// The Google account connection behind "Export to Google Slides" (the
// default export), shown on the profile view.
//
// Connecting sends the browser to Google's consent screen and back to
// /api/google/callback, which lands on /?google=connected (or =failed /
// =noscope). It needs the server's Google Cloud OAuth client: until an admin
// sets one in Settings, admins get a button there and everyone else a note.
const Google = {
  me: null,
  status: { enabled: false , connected: false , email: null },
  returned: null,

  async init( me ) {
    this.me = me;
    Dom.show( Dom.get( "google-panel" ) , true );
    Dom.get( "google-connect" ).addEventListener( "click" , this.connect.bind( this ) );
    Dom.get( "google-disconnect" ).addEventListener( "click" , this.disconnect.bind( this ) );
    Dom.get( "google-setup" ).addEventListener( "click" , function () { App.go( "settings" ); } );
    this.showReturn();
    await this.refresh();
  },

  isAdmin() {
    return Boolean( this.me && this.me.role === "admin" );
  },

  // Back from Google: say how it went, then drop the ?google= from the
  // address bar so a reload does not say it again.
  showReturn() {
    const params = new URLSearchParams( window.location.search );
    const outcome = params.get( "google" );
    if ( [ "connected" , "failed" , "noscope" ].indexOf( outcome ) < 0 ) return;
    const box = Dom.get( "google-result" );
    box.className = "notice " + ( outcome === "connected" ? "notice-ok" : "notice-error" );
    Dom.text( box , I18n.get( "google." + outcome + "_notice" ) );
    Dom.show( box , box.textContent !== "" );
    this.returned = outcome;
    window.history.replaceState( null , "" , window.location.pathname + window.location.hash );
  },

  async refresh() {
    try {
      this.status = await Api.googleStatus();
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    const status = this.status;
    const ready = status.enabled;
    Dom.text( Dom.get( "google-status" ) , !ready ? "" : status.connected
      ? I18n.format( "google.connected_as" , { email: status.email || "?" } )
      : I18n.get( "google.not_connected" ) );
    Dom.show( Dom.get( "google-status" ) , ready );
    Dom.text( Dom.get( "google-unconfigured" ) , I18n.get( this.isAdmin() ? "google.not_set_up_admin" : "google.not_set_up_user" ) );
    Dom.show( Dom.get( "google-unconfigured" ) , !ready );
    Dom.show( Dom.get( "google-setup" ) , !ready && this.isAdmin() );
    Dom.show( Dom.get( "google-connect" ) , ready && !status.connected );
    Dom.show( Dom.get( "google-disconnect" ) , ready && status.connected );
    Export.setGoogle( status );
  },

  async connect() {
    if ( this.me && this.me.auth !== "session" ) return;
    try {
      const answer = await Api.googleConnect();
      window.location.assign( answer.url );
    } catch ( error ) {
      App.showError( error.message );
    }
  },

  async disconnect() {
    const question = I18n.get( "google.disconnect_confirm" );
    if ( question !== "" && !window.confirm( question ) ) return;
    try {
      await Api.googleDisconnect();
    } catch ( error ) {
      App.showError( error.message );
    }
    Dom.show( Dom.get( "google-result" ) , false );
    await this.refresh();
  },
};
