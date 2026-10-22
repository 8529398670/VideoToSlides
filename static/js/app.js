// Page logic for the account + admin screen this template ships with.
//
// Most projects will replace much of this file. What is worth keeping is the
// shape: load the language file, ask /api/me once, then branch on
// authenticated/role. Do not re-check the session anywhere else -- the server
// is the authority, and a second client-side notion of "am I logged in" is
// how the two drift apart.
//
// New screens belong in their own file under /js/ with their own init(),
// rather than being appended here until this becomes the monolith the rest of
// the project is organised to avoid.

const App = {
  currentUser: null,

  async init() {
    try {
      await I18n.load();
      I18n.apply();
      Theme.init();
    } catch ( error ) {
      // Without the language file every label is blank, so say something in
      // the one place that can afford a hardcoded string.
      Dom.text( Dom.get( "error-banner" ) , "Could not load language file." );
      Dom.show( Dom.get( "error-banner" ) , true );
    }

    let me = { authenticated: false };
    try {
      me = await Api.me();
    } catch ( error ) {
      this.showError( I18n.get( "errors.network" ) );
    }

    this.currentUser = me.authenticated ? me : null;
    Dom.show( Dom.get( "main" ) , true );

    if ( this.loginCredential() !== "" ) {
      this.renderLoginConfirm( me );
      return;
    }
    if ( !me.authenticated ) {
      this.renderSignedOut();
      return;
    }
    await this.renderSignedIn( me );
  },

  renderSignedOut() {
    Dom.show( Dom.get( "signed-out" ) , true );
    // A failed login lands here with ?login=invalid rather than explaining
    // what was wrong with the link -- see app/routes/auth.py.
    const params = new URLSearchParams( window.location.search );
    if ( params.get( "login" ) === "invalid" ) {
      const banner = Dom.get( "invalid-link" );
      if ( I18n.get( "signed_out.invalid_link" ) !== "" ) Dom.show( banner , true );
    }
  },

  // A login link is /login/<credential>, and the server answers it with this
  // page. Nothing has been redeemed at that point.
  loginCredential() {
    const prefix = "/login/";
    const path = window.location.pathname;
    if ( path.indexOf( prefix ) !== 0 ) return "";
    try {
      return decodeURIComponent( path.slice( prefix.length ) ).replace( /\/+$/ , "" );
    } catch ( error ) {
      return "";
    }
  },

  // Opening a login link only shows this. The link is spent by the POST the
  // button sends, so a chat app's link preview or a mail scanner fetching the
  // URL cannot use it up, and someone already signed in is told they are
  // about to switch accounts rather than being switched silently.
  renderLoginConfirm( me ) {
    Dom.show( Dom.get( "login-confirm" ) , true );
    if ( me.authenticated ) {
      const warning = I18n.get( "login_confirm.switch_warning" );
      if ( warning !== "" ) {
        const box = Dom.get( "login-switch-warning" );
        Dom.text( box , warning.replace( "{name}" , me.display_name ) );
        Dom.show( box , true );
      }
    }

    const button = Dom.get( "login-confirm-button" );
    button.addEventListener( "click" , async () => {
      button.disabled = true;
      try {
        await Api.login( this.loginCredential() );
        // replace, not assign: the link should not stay in the back button.
        window.location.replace( "/" );
      } catch ( error ) {
        if ( error.status === 400 ) {
          window.location.replace( "/?login=invalid" );
          return;
        }
        // Anything else (rate limited, network) leaves the link unspent,
        // so let them try again.
        button.disabled = false;
        this.showError( error.message );
      }
    } );
  },

  async renderSignedIn( me ) {
    Dom.show( Dom.get( "signed-in" ) , true );
    this.showWho( me.display_name );
    Dom.get( "display-name" ).value = me.display_name;
    this.initNav();

    Dom.get( "rename-form" ).addEventListener( "submit" , this.onRename.bind( this ) );
    Dom.get( "logout-button" ).addEventListener( "click" , this.onLogout.bind( this ) );

    // Each screen owns its own file and its own init(); these lines are the
    // only place that knows they exist.
    await Keys.init( me );
    await Google.init( me );
    Export.init( me );
    Review.init();
    Upload.init( me );
    Youtube.init( me );
    await Jobs.init( me );
    Settings.init( me );

    if ( me.role !== "admin" ) return;

    Dom.show( Dom.get( "admin-panel" ) , true );
    Dom.get( "create-user-form" ).addEventListener( "submit" , this.onCreateUser.bind( this ) );
    Dom.get( "copy-link-button" ).addEventListener( "click" , this.onCopyLink.bind( this ) );
    await this.refreshUsers();
  },

  async onRename( event ) {
    event.preventDefault();
    const value = Dom.get( "display-name" ).value.trim();
    try {
      await Api.rename( value );
      this.currentUser.display_name = value;
      this.showWho( value );
      Dom.flash( Dom.get( "rename-saved" ) );
    } catch ( error ) {
      this.showError( error.message );
    }
  },

  async onLogout() {
    try {
      await Api.logout();
    } catch ( error ) {
      // Even if the call failed, reloading lands on the signed-out page,
      // which is the honest state to show.
    }
    window.location.href = "/";
  },

  async onCreateUser( event ) {
    event.preventDefault();
    const name = Dom.get( "new-name" ).value.trim();
    const role = Dom.get( "new-role" ).value;
    try {
      const created = await Api.createUser( name , role );
      this.showLoginLink( created.login_path );
      Dom.get( "new-name" ).value = "";
      await this.refreshUsers();
    } catch ( error ) {
      this.showError( error.message );
    }
  },

  // The server returns a path, not a full URL: behind a reverse proxy it does
  // not reliably know its own public origin, whereas the browser always does.
  showLoginLink( loginPath ) {
    Dom.text( Dom.get( "login-link-value" ) , window.location.origin + loginPath );
    Dom.show( Dom.get( "login-link-box" ) , true );
  },

  async onCopyLink() {
    await Dom.copy( Dom.get( "login-link-value" ) , Dom.get( "copied-notice" ) );
  },

  async refreshUsers() {
    const body = Dom.get( "users-table" ).querySelector( "tbody" );
    let users = [];
    try {
      users = await Api.listUsers();
    } catch ( error ) {
      this.showError( error.message );
      return;
    }

    Dom.clear( body );
    users.forEach( function ( user ) {
      const statusKey = user.disabled ? "admin.status_disabled" : "admin.status_active";

      const reissueButton = Dom.el( "button" , {
        class: "secondary small",
        text: I18n.get( "admin.reissue_button" ),
        on: { click: async function () {
          try {
            const issued = await Api.reissueLogin( user.id );
            App.showLoginLink( issued.login_path );
          } catch ( error ) { App.showError( error.message ); }
        } },
      } );

      // Revoking is for "someone else may have this account's credentials":
      // it ends every session, unused login link, and API key, and leaves the
      // account enabled. Not offered on your own row -- the server refuses
      // that, since it would sign you out with no way back in.
      const revokeButton = user.id === App.currentUser.id ? null : Dom.el( "button" , {
        class: "secondary small",
        text: I18n.get( "admin.revoke_button" ),
        on: { click: async function () {
          const question = I18n.get( "admin.revoke_confirm" ).replace( "{name}" , user.display_name );
          if ( question !== "" && !window.confirm( question ) ) return;
          try {
            await Api.revokeAccess( user.id );
            if ( App.currentUser.api_keys_enabled ) await Keys.refreshAll();
          } catch ( error ) { App.showError( error.message ); }
        } },
      } );

      const toggleButton = Dom.el( "button" , {
        class: "secondary small",
        text: I18n.get( user.disabled ? "admin.enable_button" : "admin.disable_button" ),
        on: { click: async function () {
          try {
            await Api.setDisabled( user.id , !user.disabled );
            await App.refreshUsers();
          } catch ( error ) { App.showError( error.message ); }
        } },
      } );

      // Built with createElement + textContent, never an HTML string: a
      // display name is user-controlled input.
      body.appendChild( Dom.el( "tr" , { children: [
        Dom.el( "td" , { text: user.display_name } ),
        Dom.el( "td" , { text: user.role } ),
        Dom.el( "td" , { text: I18n.get( statusKey ) } ),
        Dom.el( "td" , { children: [
          Dom.el( "div" , { class: "row-actions" , children: [ reissueButton , revokeButton , toggleButton ].filter( Boolean ) } ),
        ] } ),
      ] } ) );
    } );
  },

  // One page, three views, chosen by the URL hash so the back button and a
  // reload keep them: the videos (no hash), the profile (#account: name,
  // Google, API keys, and the admin panel for admins), and Settings
  // (#settings, admins only). The top bar has no tabs -- the title goes home,
  // and two icons open the other two.
  initNav() {
    Dom.show( Dom.get( "header-actions" ) , true );
    Dom.show( Dom.get( "settings-button" ) , this.currentUser.role === "admin" );
    Dom.get( "brand-link" ).addEventListener( "click" , function ( event ) {
      event.preventDefault();
      App.go( "" );
    } );
    Dom.get( "profile-button" ).addEventListener( "click" , function () { App.go( "account" ); } );
    Dom.get( "settings-button" ).addEventListener( "click" , function () { App.go( "settings" ); } );
    window.addEventListener( "hashchange" , this.showView.bind( this ) );
    this.showView();
  },

  go( view ) {
    // Clicking the icon of the view already open goes back to the videos.
    const current = window.location.hash.replace( "#" , "" );
    const next = current === view ? "" : view;
    if ( next === "" ) window.history.pushState( null , "" , window.location.pathname );
    else window.location.hash = next;
    this.showView();
  },

  showView() {
    const hash = window.location.hash.replace( "#" , "" );
    const isAdmin = this.currentUser && this.currentUser.role === "admin";
    const view = hash === "account" ? "account" : hash === "settings" && isAdmin ? "settings" : "convert";
    Dom.show( Dom.get( "view-convert" ) , view === "convert" );
    Dom.show( Dom.get( "view-account" ) , view === "account" );
    Dom.show( Dom.get( "view-settings" ) , view === "settings" );
    Dom.get( "profile-button" ).classList.toggle( "is-active" , view === "account" );
    Dom.get( "settings-button" ).classList.toggle( "is-active" , view === "settings" );
    if ( view === "settings" ) Settings.open();
    window.scrollTo( 0 , 0 );
  },

  // The signed-in name lives in the profile icon's tooltip now.
  showWho( name ) {
    const button = Dom.get( "profile-button" );
    const label = I18n.get( "header.profile" );
    button.setAttribute( "title" , label ? label + " — " + name : name );
  },

  showError( message ) {
    const banner = Dom.get( "error-banner" );
    Dom.text( banner , message || I18n.get( "errors.generic" ) );
    Dom.flash( banner , 5000 );
  },
};

document.addEventListener( "DOMContentLoaded" , function () {
  App.init();
} );
