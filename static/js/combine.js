// "Convert all into one": every finished video of a batch (a playlist or a
// dropped folder) in a single PowerPoint / Google Slides deck.
//
// The first time, the videos are in title order, read the way a person
// would -- "Lecture 2" before "Lecture 10". The person can drag them (or
// use the arrows, which also work on a phone) and untick any to leave out.
// The server remembers that order and those choices per batch, so the next
// time the dialog opens as they left it; videos finished since then join at
// the end. Converting again replaces the batch's Google deck, like a
// single video's.
const Combine = {
  combos: [],        // the caller's combined-deck records, from the server
  group: null,       // the batch the dialog is open for
  dragging: null,
  resumeKey: "videotoslides.resumeCombined",
  collator: new Intl.Collator( undefined , { numeric: true , sensitivity: "base" } ),

  init() {
    const dialog = Dom.get( "combine-dialog" );
    Dom.get( "combine-form" ).addEventListener( "submit" , this.onSubmit.bind( this ) );
    Dom.get( "combine-cancel" ).addEventListener( "click" , function () { dialog.close(); } );
    Dom.get( "combine-sort" ).addEventListener( "click" , function () {
      Combine.fill( Combine.byTitle( Combine.readyJobs() ) , Combine.leftOut() );
    } );
    Dom.get( "combine-gslides" ).addEventListener( "change" , this.renderGoogle.bind( this ) );
    dialog.addEventListener( "close" , function () { Combine.group = null; } );
    const list = Dom.get( "combine-list" );
    list.addEventListener( "dragover" , this.onDragOver.bind( this ) );
    list.addEventListener( "drop" , function ( event ) { event.preventDefault(); } );
  },

  async refresh() {
    try {
      this.combos = await Api.listCombined();
    } catch ( error ) { /* keep the last list */ }
  },

  forBatch( batch ) {
    return this.combos.find( function ( combo ) { return combo.batch === batch; } ) || null;
  },

  working( combo ) {
    return Object.values( combo.exports || {} ).some( function ( state ) { return state.status === "working"; } );
  },

  busy() {
    return this.combos.some( function ( combo ) { return Combine.working( combo ); } );
  },

  readyJobs() {
    return ( this.group ? this.group.jobs : [] ).filter( function ( job ) { return job.status === "ready" && job.slide_count > 0; } );
  },

  byTitle( jobs ) {
    return jobs.slice().sort( function ( a , b ) { return Combine.collator.compare( a.title , b.title ) || a.id - b.id; } );
  },

  // The saved order first, then anything not in it yet, by title.
  startOrder( jobs , combo ) {
    if ( !combo ) return this.byTitle( jobs );
    const byId = {};
    jobs.forEach( function ( job ) { byId[ job.id ] = job; } );
    const saved = combo.job_ids.filter( function ( id ) { return byId[ id ]; } );
    const fresh = jobs.filter( function ( job ) { return saved.indexOf( job.id ) < 0; } );
    return saved.map( function ( id ) { return byId[ id ]; } ).concat( this.byTitle( fresh ) );
  },

  open( group ) {
    this.group = group;
    const combo = this.forBatch( group.batch );
    const jobs = this.readyJobs();
    Dom.get( "combine-title" ).value = combo ? combo.title : group.batch;
    this.fill( this.startOrder( jobs , combo ) , combo ? combo.left_out : [] );
    const choice = Export.remembered();
    Dom.get( "combine-gslides" ).checked = choice.gslides;
    Dom.get( "combine-pptx" ).checked = choice.pptx;
    Dom.get( "combine-pdf" ).checked = choice.pdf;
    Dom.get( "combine-replace" ).checked = true;
    Dom.show( Dom.get( "combine-error" ) , false );
    this.renderGoogle();
    Dom.get( "combine-dialog" ).showModal();
  },

  fill( jobs , leftOut ) {
    const list = Dom.get( "combine-list" );
    Dom.clear( list );
    jobs.forEach( function ( job ) { list.appendChild( Combine.item( job , leftOut.indexOf( job.id ) < 0 ) ); } );
    this.renderSummary();
  },

  item( job , included ) {
    const checkbox = Dom.el( "input" , {
      attrs: { type: "checkbox" , "aria-label": I18n.format( "combine.include_label" , { title: job.title } ) },
      on: { change: function () { Combine.renderSummary(); } },
    } );
    checkbox.checked = included;
    const row = Dom.el( "li" , {
      class: "combine-item",
      attrs: { draggable: "true" , "data-job-id": String( job.id ) },
      on: {
        dragstart: function ( event ) {
          Combine.dragging = row;
          row.classList.add( "is-dragging" );
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData( "text/plain" , String( job.id ) );
        },
        dragend: function () {
          row.classList.remove( "is-dragging" );
          Combine.dragging = null;
        },
      },
      children: [
        Dom.el( "span" , { class: "combine-handle" , text: "⠿" , attrs: { "aria-hidden": "true" , title: I18n.get( "combine.drag_label" ) } } ),
        checkbox,
        Dom.el( "div" , { class: "combine-name" , children: [
          Dom.el( "span" , { text: job.title } ),
          Dom.el( "span" , { class: "muted small-text" , text: I18n.format( "combine.item_slides" , { count: job.included_count } ) } ),
        ] } ),
        this.moveButton( "↑" , "combine.up_label" , -1 ),
        this.moveButton( "↓" , "combine.down_label" , 1 ),
      ],
    } );
    row.dataset.slides = String( job.included_count || 0 );
    return row;
  },

  moveButton( symbol , labelKey , step ) {
    return Dom.el( "button" , {
      class: "secondary small",
      text: symbol,
      attrs: { type: "button" , "aria-label": I18n.get( labelKey ) , title: I18n.get( labelKey ) },
      on: { click: function ( event ) {
        const row = event.currentTarget.closest( "li" );
        const list = row.parentNode;
        if ( step < 0 && row.previousElementSibling ) list.insertBefore( row , row.previousElementSibling );
        if ( step > 0 && row.nextElementSibling ) list.insertBefore( row.nextElementSibling , row );
        event.currentTarget.focus();
      } },
    } );
  },

  // Live reordering while dragging: the dragged row goes before or after
  // whichever row the pointer is over, by which half it is in.
  onDragOver( event ) {
    if ( !this.dragging ) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const over = event.target.closest( ".combine-item" );
    if ( !over || over === this.dragging ) return;
    const box = over.getBoundingClientRect();
    const after = event.clientY > box.top + box.height / 2;
    over.parentNode.insertBefore( this.dragging , after ? over.nextElementSibling : over );
  },

  rows() {
    return Dom.all( ".combine-item" , Dom.get( "combine-list" ) );
  },

  leftOut() {
    return this.rows().filter( function ( row ) { return !row.querySelector( "input" ).checked; } )
      .map( function ( row ) { return Number( row.dataset.jobId ); } );
  },

  renderSummary() {
    let videos = 0;
    let slides = 0;
    this.rows().forEach( function ( row ) {
      const included = row.querySelector( "input" ).checked;
      row.classList.toggle( "is-left-out" , !included );
      if ( included ) { videos += 1; slides += Number( row.dataset.slides ); }
    } );
    Dom.text( Dom.get( "combine-summary" ) , I18n.format( "combine.summary" , { videos: videos , slides: slides } ) );
  },

  renderGoogle() {
    const google = Export.google;
    const available = Export.googleAvailable();
    const gslides = Dom.get( "combine-gslides" );
    gslides.disabled = !available;
    if ( !available ) {
      gslides.checked = false;
      if ( !Dom.get( "combine-pdf" ).checked ) Dom.get( "combine-pptx" ).checked = true;
    }
    const combo = this.group ? this.forBatch( this.group.batch ) : null;
    Dom.show( Dom.get( "combine-replace-row" ) , Boolean( combo && combo.gslides_url ) && gslides.checked );
    let note = "";
    if ( google.connected ) note = I18n.format( "export.google_as" , { email: google.email || "" } );
    else if ( google.enabled ) note = I18n.get( "export.google_will_connect" );
    Dom.text( Dom.get( "combine-google-note" ) , note );
    Dom.show( Dom.get( "combine-google-note" ) , note !== "" && gslides.checked );
  },

  async onSubmit( event ) {
    event.preventDefault();
    const group = this.group;
    if ( !group ) return;
    const problem = Dom.get( "combine-error" );
    const order = this.rows().map( function ( row ) { return Number( row.dataset.jobId ); } );
    const leftOut = this.leftOut();
    if ( order.length === leftOut.length ) {
      Dom.text( problem , I18n.get( "combine.choose_one" ) );
      Dom.show( problem , true );
      return;
    }
    const pptx = Dom.get( "combine-pptx" ).checked;
    const gslides = Dom.get( "combine-gslides" ).checked && Export.googleAvailable();
    const pdf = Dom.get( "combine-pdf" ).checked;
    if ( !pptx && !gslides && !pdf ) {
      Dom.text( problem , I18n.get( "export.choose_one" ) );
      Dom.show( problem , true );
      return;
    }
    // Google chosen but not connected: save the choice (and build any
    // PowerPoint / PDF) now, go through Google's consent screen, and send the
    // Google part on return.
    const connectFirst = gslides && !Export.google.connected;
    const payload = {
      batch: group.batch,
      title: Dom.get( "combine-title" ).value.trim() || group.batch,
      // Every video in the order shown, ticked or not, so the dialog
      // reopens as it was left; the deck skips the left-out ones.
      job_ids: order,
      left_out: leftOut,
      pptx: pptx,
      pdf: pdf,
      gslides: connectFirst ? false : gslides,
      gslides_new: !Dom.get( "combine-replace" ).checked,
    };
    const button = Dom.get( "combine-go" );
    button.disabled = true;
    let saved = null;
    try {
      saved = await Api.exportCombined( payload );
    } catch ( error ) {
      button.disabled = false;
      Dom.text( problem , error.message );
      Dom.show( problem , true );
      return;
    }
    button.disabled = false;
    if ( connectFirst ) {
      try {
        window.sessionStorage.setItem( this.resumeKey , JSON.stringify( { batch: saved.batch , title: saved.title ,
          job_ids: saved.job_ids , left_out: saved.left_out } ) );
      } catch ( error ) { /* the Google part then has to be started again by hand */ }
      Google.connect();
      return;
    }
    Dom.get( "combine-dialog" ).close();
    Jobs.refresh();
  },

  // Back from Google's consent screen with a combined Google export pending.
  async resumeAfterGoogle() {
    let pending = null;
    try {
      pending = JSON.parse( window.sessionStorage.getItem( this.resumeKey ) || "null" );
      window.sessionStorage.removeItem( this.resumeKey );
    } catch ( error ) { pending = null; }
    if ( !pending ) return false;
    try {
      await Api.exportCombined( Object.assign( pending , { pptx: false , pdf: false , gslides: true , gslides_new: false } ) );
    } catch ( error ) {
      App.showError( error.message );
    }
    await this.refresh();
    return true;
  },
};
