// Settings (the gear icon, admins only): every server-wide setting, built
// from /api/settings so a setting added in app/core/settings.py appears here
// with no change to this file -- only its label in language.yaml.
//
// Edits collect into one set of changes and go to the server together on
// Save. "Reset" sends null for a field, which puts it back to the default
// from config.yaml / tunables.py. The Google client secret is never sent
// back; leaving its box empty keeps the stored one.
const Settings = {
  me: null,
  data: null,
  changes: {},
  loading: false,

  init( me ) {
    this.me = me;
    if ( me.role !== "admin" ) return;
    Dom.get( "settings-form" ).addEventListener( "submit" , this.onSave.bind( this ) );
    Dom.get( "settings-discard" ).addEventListener( "click" , this.discard.bind( this ) );
  },

  async open() {
    if ( this.loading || ( this.data && Object.keys( this.changes ).length ) ) return;
    this.loading = true;
    try {
      this.data = await Api.getSettings();
      this.changes = {};
      this.render();
    } catch ( error ) {
      App.showError( error.message );
    } finally {
      this.loading = false;
    }
  },

  text( key , values ) {
    return values ? I18n.format( key , values ) : I18n.get( key );
  },

  // How a value reads in "Default: …".
  shown( field , value ) {
    if ( field.type === "bool" ) return I18n.get( value ? "settings.value_on" : "settings.value_off" );
    if ( field.type === "cores" ) return value ? String( value ) : I18n.get( "settings.cores_default" );
    if ( value === "" || value == null ) return "—";
    return String( value );
  },

  render() {
    const root = Dom.get( "settings-groups" );
    Dom.clear( root );
    this.data.groups.forEach( function ( group ) {
      const card = Dom.el( "section" , { class: "card settings-group" , children: [
        Dom.el( "h2" , { text: I18n.get( "settings.groups." + group.id ) } ),
      ] } );
      if ( group.id === "google" ) Settings.googleIntro( card );
      if ( group.id === "processing" ) Settings.machineIntro( card );
      group.fields.forEach( function ( field ) { card.appendChild( Settings.row( field ) ); } );
      root.appendChild( card );
    } );
    this.updateBar();
  },

  machineIntro( card ) {
    const machine = this.data.machine;
    if ( !machine ) return;
    const key = machine.physical && machine.physical !== machine.cores ? "settings.machine_physical" : "settings.machine";
    card.appendChild( Dom.el( "p" , { class: "muted small-text" , text: I18n.format( key , machine ) } ) );
  },

  // A core count for the slider's readout; 0 (stored) is "no limit".
  coresText( count , total ) {
    if ( !count || count >= total ) return I18n.format( "settings.cores_all" , { total: total } );
    return I18n.format( "settings.cores_value" , { count: count , total: total } );
  },

  googleIntro( card ) {
    card.appendChild( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "settings.google_steps" ) } ) );
    const code = Dom.el( "code" , { text: this.data.google_redirect_effective } );
    const copied = Dom.el( "span" , { class: "muted small-text" , text: I18n.get( "google.copied_notice" ) , attrs: { hidden: "" } } );
    card.appendChild( Dom.el( "div" , { class: "redirect-box" , children: [
      Dom.el( "span" , { class: "small-text" , text: I18n.get( "settings.redirect_hint" ) } ),
      Dom.el( "span" , { class: "copy-line" , children: [
        code,
        Dom.el( "button" , {
          class: "secondary small",
          text: I18n.get( "google.copy_button" ) || I18n.get( "api_keys.copy_button" ),
          attrs: { type: "button" },
          on: { click: function () { Dom.copy( code , copied ); } },
        } ),
        copied,
      ] } ),
    ] } ) );
  },

  input( field , id ) {
    if ( field.type === "bool" ) {
      const box = Dom.el( "input" , { attrs: { type: "checkbox" , id: id } } );
      box.checked = Boolean( field.value );
      box.addEventListener( "change" , function () { Settings.set( field , box.checked ); } );
      return box;
    }
    if ( field.type === "choice" ) {
      const select = Dom.el( "select" , { attrs: { id: id } , children: field.choices.map( function ( choice ) {
        return Dom.el( "option" , { text: String( choice ) , attrs: { value: String( choice ) } } );
      } ) } );
      select.value = String( field.value );
      select.addEventListener( "change" , function () { Settings.set( field , Number( select.value ) ); } );
      return select;
    }
    if ( field.type === "cores" ) {
      // The right end means no limit, stored as 0.
      const total = field.max;
      const slider = Dom.el( "input" , { class: "cores-slider" , attrs: {
        type: "range" , id: id , min: "1" , max: String( total ) , step: "1" ,
      } } );
      slider.value = String( field.value && field.value < total ? field.value : total );
      const readout = Dom.el( "output" , { class: "small-text" , attrs: { for: id } } );
      const show = function () {
        const count = Number( slider.value );
        Dom.text( readout , Settings.coresText( count , total ) );
        slider.setAttribute( "aria-valuetext" , readout.textContent );
      };
      show();
      slider.addEventListener( "input" , function () {
        show();
        const count = Number( slider.value );
        Settings.set( field , count >= total ? 0 : count );
      } );
      return Dom.el( "div" , { class: "cores-input" , children: [ slider , readout ] } );
    }
    if ( field.type === "str" || field.type === "secret" ) {
      const attrs = { type: field.type === "secret" ? "password" : "text" , id: id , spellcheck: "false" };
      if ( field.type === "secret" ) {
        attrs.placeholder = I18n.get( field.set ? "settings.secret_set" : "settings.secret_unset" );
        attrs.autocomplete = "new-password";
      } else if ( field.key === "google_redirect_url" ) {
        attrs.placeholder = this.data.google_redirect_default;
      }
      const box = Dom.el( "input" , { attrs: attrs } );
      box.value = field.type === "secret" ? "" : String( field.value || "" );
      if ( field.key === "google_redirect_url" && field.value === this.data.google_redirect_default && !field.overridden ) box.value = "";
      box.addEventListener( "input" , function () { Settings.set( field , box.value ); } );
      return box;
    }
    const attrs = { type: "number" , id: id , inputmode: field.type === "int" ? "numeric" : "decimal" };
    if ( field.min != null ) attrs.min = String( field.min );
    if ( field.max != null ) attrs.max = String( field.max );
    if ( field.step != null ) attrs.step = String( field.step );
    const box = Dom.el( "input" , { attrs: attrs } );
    box.value = String( field.value );
    box.addEventListener( "input" , function () {
      Settings.set( field , box.value === "" ? undefined : Number( box.value ) );
    } );
    return box;
  },

  row( field ) {
    const id = "setting-" + field.key;
    const label = I18n.get( "settings.fields." + field.key + ".label" ) || field.key;
    const help = I18n.get( "settings.fields." + field.key + ".help" );
    const reset = Dom.el( "button" , {
      class: "secondary small",
      text: I18n.get( "settings.reset_button" ),
      attrs: { type: "button" },
      on: { click: function () { Settings.reset( field , row ); } },
    } );
    reset.hidden = !field.overridden;
    const meta = Dom.el( "span" , { class: "muted small-text" , text: field.type === "secret" ? "" :
      I18n.format( "settings.default_label" , { value: Settings.shown( field , field.default ) } ) } );
    const changed = Dom.el( "span" , { class: "badge setting-changed" , text: I18n.get( "settings.changed_label" ) } );
    changed.hidden = !field.overridden;
    const row = Dom.el( "div" , { class: "setting-row" + ( field.type === "bool" ? " is-bool" : "" ) , children: [
      Dom.el( "div" , { class: "setting-text" , children: [
        Dom.el( "label" , { text: label , attrs: { for: id } } ),
        help ? Dom.el( "p" , { class: "muted small-text" , text: help } ) : null,
        Dom.el( "div" , { class: "setting-meta" , children: [ meta , changed , reset ] } ),
      ].filter( Boolean ) } ),
      Dom.el( "div" , { class: "setting-input" , children: [ this.input( field , id ) ] } ),
    ] } );
    row.setAttribute( "data-key" , field.key );
    return row;
  },

  set( field , value ) {
    if ( value === undefined ) {
      delete this.changes[ field.key ];
    } else if ( field.type !== "secret" && value === field.value && !( field.key in this.changes && this.changes[ field.key ] === null ) ) {
      delete this.changes[ field.key ];
    } else if ( field.type === "secret" && value === "" ) {
      delete this.changes[ field.key ];
    } else {
      this.changes[ field.key ] = value;
    }
    this.markRow( field.key );
    this.updateBar();
  },

  reset( field , row ) {
    this.changes[ field.key ] = null;
    const box = row.querySelector( "input, select" );
    if ( field.type === "cores" ) {
      box.value = String( field.default && field.default < field.max ? field.default : field.max );
      box.dispatchEvent( new Event( "input" ) );
      this.changes[ field.key ] = null;
    } else if ( field.type === "bool" ) box.checked = Boolean( field.default );
    else if ( field.type === "secret" || field.key === "google_redirect_url" ) box.value = "";
    else box.value = String( field.default );
    this.markRow( field.key );
    this.updateBar();
  },

  markRow( key ) {
    const row = Dom.get( "settings-groups" ).querySelector( '[data-key="' + key + '"]' );
    if ( row ) row.classList.toggle( "is-dirty" , key in this.changes );
  },

  updateBar() {
    const dirty = Object.keys( this.changes ).length > 0;
    Dom.get( "settings-save" ).disabled = !dirty;
    Dom.get( "settings-discard" ).disabled = !dirty;
    Dom.text( Dom.get( "settings-status" ) , dirty ? I18n.get( "settings.unsaved_notice" ) : "" );
  },

  discard() {
    this.changes = {};
    this.render();
  },

  async onSave( event ) {
    event.preventDefault();
    const button = Dom.get( "settings-save" );
    button.disabled = true;
    try {
      this.data = await Api.saveSettings( this.changes );
      this.changes = {};
      this.render();
      Dom.text( Dom.get( "settings-status" ) , I18n.get( "settings.saved_notice" ) );
      this.afterSave();
    } catch ( error ) {
      App.showError( error.message );
      this.updateBar();
    }
  },

  // Settings that change what the rest of the page offers.
  afterSave() {
    const field = function ( key ) {
      for ( const group of Settings.data.groups ) {
        for ( const entry of group.fields ) if ( entry.key === key ) return entry;
      }
      return null;
    };
    const youtube = field( "youtube_enabled" );
    if ( youtube ) {
      Dom.show( Dom.get( "youtube-form" ) , youtube.value );
      Dom.show( Dom.get( "youtube-hint" ) , youtube.value && I18n.get( "convert.youtube_hint" ) !== "" );
    }
    Google.refresh();
  },
};
