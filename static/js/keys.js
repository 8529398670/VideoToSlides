// The API keys screen: what has standing, non-browser access to this app.
//
// Its own file with its own init(), per the convention in the frontend
// reference -- app.js stays the account/admin screen rather than growing a
// second one. app.js calls Keys.init( me ) once it knows who is signed in.
//
// Two sections, and the difference matters: "my keys" is for every signed-in
// user, and the all-keys table is the admin's answer to "who and what can get
// in". A key created here carries a role from the same set users have, and can
// never be given more than the account that owns it.
const Keys = {
  me: null,

  async init( me ) {
    this.me = me;
    // The server can switch the whole feature off (auth.api_keys: false in config.yaml). When it
    // has, the endpoints are gone, so showing the panel would only produce a
    // form that 404s.
    if ( me.api_keys_enabled === false ) return;

    Dom.show( Dom.get( "api-keys-panel" ) , true );
    // Only an admin is offered the role choice. A non-admin has exactly one
    // option, and an "admin" entry the server would refuse is worse than no
    // choice at all.
    Dom.show( Dom.get( "key-role-field" ) , me.role === "admin" );
    Dom.get( "create-key-form" ).addEventListener( "submit" , this.onCreate.bind( this ) );
    Dom.get( "copy-key-button" ).addEventListener( "click" , this.onCopy.bind( this ) );
    await this.refresh();

    if ( me.role !== "admin" ) return;
    Dom.show( Dom.get( "admin-keys-panel" ) , true );
    await this.refreshAll();
  },

  // A key is shown once, on creation, and is unrecoverable afterwards -- the
  // server stores only a hash. Saying so next to the value is the whole reason
  // this box exists rather than a toast that disappears.
  showSecret( credential ) {
    Dom.text( Dom.get( "key-secret-value" ) , credential );
    Dom.show( Dom.get( "key-secret-box" ) , true );
  },

  async onCopy() {
    await Dom.copy( Dom.get( "key-secret-value" ) , Dom.get( "key-copied-notice" ) );
  },

  async onCreate( event ) {
    event.preventDefault();
    const name = Dom.get( "key-name" ).value.trim();
    const role = this.me.role === "admin" ? Dom.get( "key-role" ).value : "user";

    // An empty field means "no opinion", which the server reads as its
    // configured default. Sending 0 instead would ask for a key that never
    // expires, which is a different thing and has to be typed on purpose.
    const rawDays = Dom.get( "key-days" ).value.trim();
    const expiresInDays = rawDays === "" ? null : Number( rawDays );

    try {
      const created = await Api.createKey( name , role , expiresInDays );
      this.showSecret( created.credential );
      Dom.get( "key-name" ).value = "";
      await this.refresh();
      if ( this.me.role === "admin" ) await this.refreshAll();
    } catch ( error ) {
      App.showError( error.message );
    }
  },

  // Dates are rendered in the viewer's own timezone, and a key with no expiry
  // says so rather than showing a blank cell.
  formatWhen( value ) {
    if ( !value ) return I18n.get( "api_keys.never" );
    const parsed = new Date( value );
    if ( isNaN( parsed.getTime() ) ) return I18n.get( "api_keys.never" );
    return parsed.toLocaleString();
  },

  statusText( key ) {
    if ( key.revoked ) return I18n.get( "api_keys.status_revoked" );
    if ( key.expired ) return I18n.get( "api_keys.status_expired" );
    return I18n.get( "api_keys.status_active" );
  },

  // revokeButton is shared by both tables: the only difference is which
  // endpoint kills the key, so that is the parameter.
  revokeButton( key , revoke ) {
    if ( !key.live ) return Dom.el( "span" , { class: "muted" , text: "" } );
    return Dom.el( "button" , {
      class: "secondary small",
      text: I18n.get( "api_keys.revoke_button" ),
      on: { click: async function () {
        // An empty confirm message means "do not ask", following the same
        // language.yaml rule as every other string here.
        const question = I18n.get( "api_keys.revoke_confirm" );
        if ( question !== "" && !window.confirm( question ) ) return;
        try {
          await revoke( key.id );
          await Keys.refresh();
          if ( Keys.me.role === "admin" ) await Keys.refreshAll();
        } catch ( error ) { App.showError( error.message ); }
      } },
    } );
  },

  async refresh() {
    const body = Dom.get( "keys-table" ).querySelector( "tbody" );
    let keys = [];
    try {
      keys = await Api.listKeys();
    } catch ( error ) {
      App.showError( error.message );
      return;
    }

    Dom.clear( body );
    keys.forEach( function ( key ) {
      // createElement + textContent throughout: a key's name is free text its
      // owner typed. See the note at the top of dom.js.
      body.appendChild( Dom.el( "tr" , { children: [
        Dom.el( "td" , { text: key.name } ),
        Dom.el( "td" , { text: key.role } ),
        Dom.el( "td" , { text: Keys.statusText( key ) } ),
        Dom.el( "td" , { text: Keys.formatWhen( key.last_used_at ) } ),
        Dom.el( "td" , { text: Keys.formatWhen( key.expires_at ) } ),
        Dom.el( "td" , { children: [
          Dom.el( "div" , { class: "row-actions" , children: [
            Keys.revokeButton( key , function ( id ) { return Api.revokeKey( id ); } ),
          ] } ),
        ] } ),
      ] } ) );
    } );
  },

  async refreshAll() {
    const body = Dom.get( "admin-keys-table" ).querySelector( "tbody" );
    let keys = [];
    try {
      keys = await Api.listAllKeys();
    } catch ( error ) {
      App.showError( error.message );
      return;
    }

    Dom.clear( body );
    keys.forEach( function ( key ) {
      body.appendChild( Dom.el( "tr" , { children: [
        Dom.el( "td" , { text: key.display_name || ( "#" + key.user_id ) } ),
        Dom.el( "td" , { text: key.name } ),
        Dom.el( "td" , { text: key.role } ),
        Dom.el( "td" , { text: Keys.statusText( key ) } ),
        Dom.el( "td" , { text: Keys.formatWhen( key.last_used_at ) } ),
        Dom.el( "td" , { children: [
          Dom.el( "div" , { class: "row-actions" , children: [
            Keys.revokeButton( key , function ( id ) { return Api.revokeAnyKey( id ); } ),
          ] } ),
        ] } ),
      ] } ) );
    } );
  },
};
