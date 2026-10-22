// The job list: every video this user has sent, what it is doing, and what
// can be done with it.
//
// It polls /api/jobs every couple of seconds while anything is moving (a job
// running, an export building, an upload in flight) and stops when nothing
// is. When a job it has watched turns "ready", it opens the export prompt --
// that is the "convert to PowerPoint and/or Google Slides?" moment. Videos
// from a playlist or a dropped folder do not prompt one by one: their batch
// shows a progress bar instead, and is converted from its own buttons.
//
// Each batch is a collapsed accordion; which ones are open is remembered in
// this browser, as a convenience.
const Jobs = {
  me: null,
  jobs: [],
  seen: {},          // job id -> last status, to spot the move to "ready"
  deleting: null,    // what the delete dialog is asking about (see openDelete)
  openBatches: null, // batch names expanded by the person
  openKey: "videotoslides.openBatches",
  loaded: false,
  timer: null,
  lastRender: "",
  busyInterval: 2000,
  idleInterval: 30000,

  async init( me ) {
    this.me = me;
    this.initDeleteDialog();
    this.openBatches = this.loadOpen();
    Combine.init();
    document.addEventListener( "visibilitychange" , function () {
      if ( !document.hidden ) Jobs.refresh();
    } );
    await this.refresh();
    // Back from Google's consent screen: reopen the prompt that sent the
    // person there, or else show the Account view, where the outcome is.
    if ( Google.returned ) {
      const connected = Google.returned === "connected";
      const resumedCombined = connected && await Combine.resumeAfterGoogle();
      const resumed = connected && ( await Export.resumeAfterGoogle( this.jobs ) || resumedCombined );
      if ( resumedCombined ) this.refresh();
      if ( !resumed ) {
        App.go( "account" );
      }
    }
  },

  busy() {
    return Upload.busy() || Combine.busy() || this.jobs.some( function ( job ) {
      return Jobs.isActive( job ) || Jobs.exportWorking( job );
    } );
  },

  isActive( job ) {
    return [ "queued" , "fetching" , "starting" , "downloading" , "scanning" , "extracting" , "filtering" ].indexOf( job.status ) >= 0;
  },

  exportWorking( job ) {
    return Object.values( job.exports || {} ).some( function ( state ) { return state.status === "working"; } );
  },

  async refresh() {
    window.clearTimeout( this.timer );
    let list = null;
    try {
      [ list ] = await Promise.all( [ Api.listJobs() , Combine.refresh() ] );
    } catch ( error ) {
      if ( error.status === 401 ) { window.location.href = "/"; return; }
    }
    if ( list ) {
      const newlyReady = [];
      // A batch of one is shown as a plain video, so it prompts like one.
      const batchSize = {};
      list.forEach( function ( job ) { if ( job.batch ) batchSize[ job.batch ] = ( batchSize[ job.batch ] || 0 ) + 1; } );
      list.forEach( function ( job ) {
        const before = Jobs.seen[ job.id ];
        // Only jobs seen running on this page prompt: reloading the page
        // must not re-ask about everything that ever finished.
        if ( Jobs.loaded && job.status === "ready" && before !== undefined && before !== "ready" && !( batchSize[ job.batch ] > 1 ) ) newlyReady.push( job );
        Jobs.seen[ job.id ] = job.status;
      } );
      this.jobs = list;
      this.loaded = true;
      this.render();
      if ( newlyReady.length ) Export.prompt( newlyReady.filter( function ( job ) { return job.slide_count > 0; } ) );
    }
    if ( document.hidden ) return;
    this.timer = window.setTimeout( function () { Jobs.refresh(); } , this.busy() ? this.busyInterval : this.idleInterval );
  },

  find( jobId ) {
    return this.jobs.find( function ( job ) { return job.id === jobId; } ) || null;
  },

  // Group by batch (a playlist or a dropped folder), keeping newest first.
  groups() {
    const order = [];
    const byKey = {};
    this.jobs.forEach( function ( job ) {
      const key = job.batch ? "batch:" + job.batch : "job:" + job.id;
      if ( !byKey[ key ] ) {
        byKey[ key ] = { batch: job.batch , jobs: [] };
        order.push( byKey[ key ] );
      }
      byKey[ key ].jobs.push( job );
    } );
    return order;
  },

  render() {
    // Polling re-fetches constantly; rebuilding identical DOM would only
    // steal focus and flicker images.
    const snapshot = JSON.stringify( [ this.jobs , Combine.combos , Array.from( this.openBatches ) ] );
    if ( snapshot === this.lastRender ) return;
    this.lastRender = snapshot;

    const list = Dom.get( "jobs-list" );
    Dom.clear( list );
    Dom.show( Dom.get( "jobs-empty" ) , this.jobs.length === 0 );
    this.groups().forEach( function ( group ) {
      if ( !group.batch || group.jobs.length < 2 ) {
        group.jobs.forEach( function ( job ) { list.appendChild( Jobs.row( job ) ); } );
        return;
      }
      list.appendChild( Jobs.batch( group ) );
    } );
  },

  loadOpen() {
    try {
      const saved = JSON.parse( window.localStorage.getItem( this.openKey ) || "[]" );
      if ( Array.isArray( saved ) ) return new Set( saved.filter( function ( name ) { return typeof name === "string"; } ) );
    } catch ( error ) { /* all collapsed */ }
    return new Set();
  },

  toggleBatch( name ) {
    if ( this.openBatches.has( name ) ) this.openBatches.delete( name );
    else this.openBatches.add( name );
    try {
      window.localStorage.setItem( this.openKey , JSON.stringify( Array.from( this.openBatches ) ) );
    } catch ( error ) { /* not remembered */ }
    this.render();
    // The list was rebuilt; put focus back on the header that was pressed.
    const header = Dom.all( ".batch-toggle" ).find( function ( button ) { return button.dataset.batch === name; } );
    if ( header ) header.focus();
  },

  // How far a video is, 0..1, across all its stages -- a stage's own
  // progress restarts at 0, so each stage gets a share of the bar.
  // A video downloaded ahead has its download share done while it waits.
  stageShare: { queued: [ 0 , 0 ] , fetching: [ 0 , 0.2 ] , starting: [ 0.2 , 0 ] , downloading: [ 0.02 , 0.18 ] ,
    scanning: [ 0.2 , 0.55 ] , extracting: [ 0.75 , 0.15 ] , filtering: [ 0.9 , 0.1 ] },

  completion( job ) {
    if ( job.status === "queued" && job.downloaded ) return 0.2;
    const share = this.stageShare[ job.status ];
    if ( !share ) return 1;
    return share[ 0 ] + share[ 1 ] * Math.max( 0 , Math.min( 1 , job.progress || 0 ) );
  },

  batch( group ) {
    const total = group.jobs.length;
    const ready = group.jobs.filter( function ( job ) { return job.status === "ready" && job.slide_count > 0; } );
    const done = group.jobs.filter( function ( job ) { return !Jobs.isActive( job ); } ).length;
    const failed = group.jobs.filter( function ( job ) { return job.status === "failed"; } ).length;
    const running = done < total;
    const open = this.openBatches.has( group.batch );
    const bodyId = "batch-" + group.jobs[ 0 ].id;

    const status = [ I18n.format( "jobs.batch_progress" , { done: done , total: total } ) ];
    if ( failed ) status.push( I18n.format( "jobs.batch_failed" , { count: failed } ) );
    const toggle = Dom.el( "button" , {
      class: "batch-toggle",
      attrs: { type: "button" , "aria-expanded": String( open ) , "aria-controls": bodyId , "data-batch": group.batch },
      on: { click: function () { Jobs.toggleBatch( group.batch ); } },
      children: [
        Dom.el( "span" , { class: "batch-chevron" , attrs: { "aria-hidden": "true" } } ),
        Dom.el( "span" , { class: "batch-heading" , children: [
          Dom.el( "h3" , { text: group.batch } ),
          Dom.el( "span" , { class: "muted small-text" , text: status.join( " · " ) } ),
        ] } ),
      ],
    } );
    const converting = ready.length < 2 ? [] : [
      Dom.el( "button" , {
        class: "small",
        text: I18n.format( "jobs.convert_all" , { count: ready.length } ),
        attrs: { type: "button" },
        on: { click: function () { Export.prompt( ready ); } },
      } ),
      this.button( "jobs.convert_all_one" , function () { Combine.open( group ); } ),
    ];
    const buttons = Dom.el( "div" , { class: "batch-buttons" , children: converting.concat( [
      this.button( "jobs.delete_batch_button" , function () { Jobs.confirmDeleteBatch( group ); } ),
    ] ) } );

    let bar = null;
    if ( running ) {
      const fraction = group.jobs.reduce( function ( sum , job ) { return sum + Jobs.completion( job ); } , 0 ) / total;
      bar = Dom.el( "progress" , { class: "batch-progress" , attrs: {
        max: "100" , value: String( Math.round( fraction * 100 ) ) ,
        "aria-label": I18n.format( "jobs.batch_progress" , { done: done , total: total } ),
      } } );
    }
    const combo = Combine.forBatch( group.batch );
    const combinedLines = combo ? this.combinedLines( combo ) : [];
    const body = Dom.el( "div" , { class: "batch-jobs" , attrs: { id: bodyId } ,
      children: group.jobs.map( function ( job ) { return Jobs.row( job ); } ) } );
    body.hidden = !open;
    return Dom.el( "section" , { class: "batch" + ( open ? " is-open" : "" ) , children: [
      Dom.el( "div" , { class: "batch-head" , children: [ toggle , buttons ].filter( Boolean ) } ),
      bar,
      combinedLines.length ? Dom.el( "div" , { class: "batch-combined" , children: combinedLines } ) : null,
      body,
    ].filter( Boolean ) } );
  },

  // The batch's all-in-one deck: building / links / what went wrong, like a
  // single video's exportLines.
  combinedLines( combo ) {
    const lines = [];
    const pptx = ( combo.exports || {} ).pptx;
    const gslides = ( combo.exports || {} ).gslides;
    const links = [];
    [ [ pptx , "pptx" , Api.combinedPptxUrl( combo ) ] , [ ( combo.exports || {} ).pdf , "pdf" , Api.combinedPdfUrl( combo ) ] ].forEach( function ( entry ) {
      const state = entry[ 0 ];
      const kind = entry[ 1 ];
      if ( !state ) return;
      if ( state.status === "working" ) lines.push( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "combine." + kind + "_working" ) } ) );
      if ( state.status === "done" ) links.push( Jobs.link( "combine." + kind + "_download" , entry[ 2 ] , { download: "" } ) );
      if ( state.status === "failed" ) lines.push( Dom.el( "p" , { class: "notice notice-error" , text: I18n.format( "combine." + kind + "_failed" , { error: state.error || "" } ) } ) );
    } );
    if ( gslides ) {
      if ( gslides.status === "working" ) lines.push( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "combine.gslides_working" ) } ) );
      if ( gslides.status === "failed" ) lines.push( Dom.el( "p" , { class: "notice notice-error" , text: I18n.format( "combine.gslides_failed" , { error: gslides.error || "" } ) } ) );
    }
    if ( combo.gslides_url && !( gslides && gslides.status === "working" ) ) {
      links.unshift(
        this.link( "combine.gslides_open" , combo.gslides_url , { target: "_blank" , rel: "noopener noreferrer" } , "primary" ),
        this.button( "export.gslides_delete" , function () {
          Jobs.act( function () { return Api.deleteCombinedGslides( combo.id ); } ,
            "jobs.combined_gslides_delete_confirm" , combo.title );
        } ),
      );
    }
    if ( links.length ) lines.push( Dom.el( "div" , { class: "job-gslides" , children: links } ) );
    if ( lines.length ) lines.unshift( Dom.el( "p" , { class: "small-text" , text: I18n.get( "jobs.combined_label" ) + " — " + combo.title } ) );
    return lines;
  },

  statusText( job ) {
    if ( job.status === "ready" ) {
      if ( job.slide_count === 0 ) return I18n.get( "jobs.no_slides" );
      // What goes into the deck, not every capture.
      const count = job.included_count;
      return I18n.format( count === 1 ? "jobs.slides_one" : "jobs.slides" , { count: count } );
    }
    if ( job.status === "queued" && job.downloaded ) return I18n.get( "jobs.status_downloaded" );
    const label = I18n.get( "jobs.status_" + job.status ) || job.status;
    if ( [ "fetching" , "downloading" , "scanning" , "extracting" , "filtering" ].indexOf( job.status ) >= 0 ) {
      return label + " " + Math.round( job.progress * 100 ) + "%";
    }
    return label;
  },

  meta( job ) {
    const parts = [ I18n.get( job.source_kind === "youtube" ? "jobs.source_youtube" : "jobs.source_upload" ) ];
    if ( job.duration_sec ) parts.push( I18n.clock( job.duration_sec ) );
    if ( job.status === "ready" ) {
      const builds = job.hidden_build_steps || 0;
      const manual = job.slide_count - job.included_count - builds;
      if ( builds > 0 ) parts.push( I18n.format( "jobs.build_steps_hidden" , { count: builds } ) );
      if ( manual > 0 ) parts.push( I18n.format( "jobs.excluded" , { count: manual } ) );
    }
    return parts.filter( Boolean ).join( " · " );
  },

  button( key , handler , style ) {
    return Dom.el( "button" , {
      class: style || "secondary small",
      text: I18n.get( key ),
      attrs: { type: "button" },
      on: { click: handler },
    } );
  },

  link( key , href , options , style ) {
    const attrs = Object.assign( { href: href } , options || {} );
    return Dom.el( "a" , { class: "button " + ( style || "secondary" ) , text: I18n.get( key ) , attrs: attrs } );
  },

  // One line per export: building / a link to the result / what went wrong.
  exportLines( job ) {
    const lines = [];
    const pptx = ( job.exports || {} ).pptx;
    const gslides = ( job.exports || {} ).gslides;
    const downloads = [];
    [ [ pptx , "pptx" , Api.pptxUrl( job ) ] , [ ( job.exports || {} ).pdf , "pdf" , Api.pdfUrl( job ) ] ].forEach( function ( entry ) {
      const state = entry[ 0 ];
      const kind = entry[ 1 ];
      if ( !state ) return;
      if ( state.status === "working" ) lines.push( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "export." + kind + "_working" ) } ) );
      if ( state.status === "done" ) downloads.push( Jobs.link( "export." + kind + "_download" , entry[ 2 ] , { download: "" } ) );
      if ( state.status === "failed" ) lines.push( Dom.el( "p" , { class: "notice notice-error" , text: I18n.format( "export." + kind + "_failed" , { error: state.error || "" } ) } ) );
    } );
    if ( downloads.length ) lines.push( Dom.el( "div" , { class: "job-gslides" , children: downloads } ) );
    if ( gslides ) {
      if ( gslides.status === "working" ) lines.push( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "export.gslides_working" ) } ) );
      if ( gslides.status === "failed" ) lines.push( Dom.el( "p" , { class: "notice notice-error" , text: I18n.format( "export.gslides_failed" , { error: gslides.error || "" } ) } ) );
    }
    // The job's deck, with a button that trashes just the deck. The deck
    // outlives a re-run, so this shows whenever one exists and is not being
    // replaced right now.
    if ( job.gslides_url && !( gslides && gslides.status === "working" ) ) {
      lines.push( Dom.el( "div" , { class: "job-gslides" , children: [
        this.link( "export.gslides_open" , job.gslides_url , { target: "_blank" , rel: "noopener noreferrer" } , "primary" ),
        this.button( "export.gslides_delete" , function () {
          Jobs.act( function () { return Api.deleteGslides( job.id ); } , "export.gslides_delete_confirm" , job.title );
        } ),
      ] } ) );
    }
    return lines;
  },

  initDeleteDialog() {
    const dialog = Dom.get( "delete-dialog" );
    Dom.get( "delete-cancel" ).addEventListener( "click" , function () { dialog.close(); } );
    Dom.get( "delete-form" ).addEventListener( "submit" , this.onDeleteSubmit.bind( this ) );
    dialog.addEventListener( "close" , function () { Jobs.deleting = null; } );
  },

  // Deleting a job with a Google deck asks whether the deck goes too; the
  // answer starts at no, every time. Without a deck the usual question is
  // asked -- or none, if that text is empty.
  confirmDelete( job ) {
    this.openDelete( {
      title: I18n.get( "jobs.delete_title" ),
      question: I18n.format( "jobs.delete_confirm" , { title: job.title } ),
      googleText: I18n.get( "jobs.delete_google_label" ),
      decks: job.gslides_url ? 1 : 0,
      call: function ( deleteGoogle ) { return Api.deleteJob( job.id , deleteGoogle ); },
    } );
  },

  // A whole batch: every video in it and its combined deck. Google decks
  // stay unless the box is ticked.
  confirmDeleteBatch( group ) {
    const combo = Combine.forBatch( group.batch );
    const decks = group.jobs.filter( function ( job ) { return job.gslides_url; } ).length + ( combo && combo.gslides_url ? 1 : 0 );
    this.openDelete( {
      title: I18n.get( "jobs.delete_batch_title" ),
      question: I18n.format( "jobs.delete_batch_confirm" , { title: group.batch , count: group.jobs.length } ),
      googleText: I18n.format( "jobs.delete_batch_google_label" , { count: decks } ),
      decks: decks,
      call: function ( deleteGoogle ) { return Api.deleteBatch( group.batch , deleteGoogle ); },
    } );
  },

  openDelete( target ) {
    if ( !target.decks && target.question === "" ) {
      this.act( function () { return target.call( false ); } );
      return;
    }
    this.deleting = target;
    Dom.text( Dom.get( "delete-title" ) , target.title );
    Dom.text( Dom.get( "delete-question" ) , target.question );
    Dom.show( Dom.get( "delete-question" ) , target.question !== "" );
    Dom.text( Dom.get( "delete-google-text" ) , target.googleText );
    Dom.get( "delete-google" ).checked = false;
    Dom.show( Dom.get( "delete-google-row" ) , target.decks > 0 );
    Dom.show( Dom.get( "delete-error" ) , false );
    Dom.get( "delete-go" ).disabled = false;
    Dom.get( "delete-dialog" ).showModal();
  },

  async onDeleteSubmit( event ) {
    event.preventDefault();
    const target = this.deleting;
    if ( !target ) return;
    const deleteGoogle = target.decks > 0 && Dom.get( "delete-google" ).checked;
    const button = Dom.get( "delete-go" );
    button.disabled = true;
    try {
      await target.call( deleteGoogle );
    } catch ( error ) {
      // Nothing was deleted; say why and leave the choice open.
      button.disabled = false;
      Dom.text( Dom.get( "delete-error" ) , error.message );
      Dom.show( Dom.get( "delete-error" ) , true );
      return;
    }
    Dom.get( "delete-dialog" ).close();
    this.refresh();
  },

  async act( call , confirmKey , title ) {
    if ( confirmKey ) {
      const question = I18n.format( confirmKey , { title: title } );
      if ( question !== "" && !window.confirm( question ) ) return;
    }
    try {
      await call();
    } catch ( error ) {
      App.showError( error.message );
    }
    this.refresh();
  },

  row( job ) {
    const actions = [];
    if ( this.isActive( job ) ) {
      actions.push( this.button( "jobs.cancel_button" , function () {
        Jobs.act( function () { return Api.cancelJob( job.id ); } );
      } ) );
    }
    if ( job.status === "ready" && job.slide_count > 0 ) {
      actions.push( this.button( "jobs.convert_button" , function () { Export.prompt( [ job ] ); } , "small" ) );
      actions.push( this.button( "jobs.review_button" , function () { Review.open( job.id ); } ) );
      actions.push( this.link( "jobs.images_button" , Api.imagesUrl( job ) , { download: "" } ) );
    }
    if ( job.status === "failed" || job.status === "cancelled" ) {
      actions.push( this.button( "jobs.retry_button" , function () {
        Jobs.act( function () { return Api.retryJob( job.id ); } );
      } ) );
    }
    // Finished jobs can be processed again, e.g. after the pipeline improves.
    if ( job.status === "ready" && job.can_rerun ) {
      actions.push( this.button( "jobs.rerun_button" , function () {
        Jobs.act( function () { return Api.retryJob( job.id ); } , "jobs.rerun_confirm" , job.title );
      } ) );
    }
    actions.push( this.button( "jobs.delete_button" , function () { Jobs.confirmDelete( job ); } ) );

    const preview = job.status === "ready" && job.slide_count > 0
      ? Dom.el( "button" , {
        class: "job-thumb",
        attrs: { type: "button" , "aria-label": I18n.get( "jobs.review_button" ) },
        on: { click: function () { Review.open( job.id ); } },
        children: [ Dom.el( "img" , { attrs: { src: Api.thumbUrl( job , 1 ) , alt: "" , loading: "lazy" } } ) ],
      } )
      : Dom.el( "div" , { class: "job-thumb job-thumb-empty" } );

    const body = [
      Dom.el( "div" , { class: "job-title" , text: job.title } ),
      Dom.el( "div" , { class: "muted small-text" , text: this.meta( job ) } ),
      Dom.el( "div" , { class: "job-status" , children: [
        Dom.el( "span" , { class: "badge badge-" + job.status , text: this.statusText( job ) } ),
      ] } ),
    ];
    if ( this.isActive( job ) && job.status !== "queued" ) {
      body.push( Dom.el( "progress" , { attrs: { max: "100" , value: String( Math.round( job.progress * 100 ) ) } } ) );
    }
    // Lots of picture changes but (almost) all of them dropped as talking-head
    // noise: the video is most likely a camera recording of a person, which
    // this detector is not built for. Say so, rather than look broken.
    const counts = job.counts || {};
    if ( job.status === "ready" && job.slide_count <= 2 && ( counts.candidates || 0 ) >= 40 ) {
      const hint = I18n.get( "jobs.camera_hint" );
      if ( hint !== "" ) body.push( Dom.el( "p" , { class: "muted small-text" , text: hint } ) );
    }
    if ( job.status === "failed" && job.error ) {
      body.push( Dom.el( "p" , { class: "notice notice-error" , text: job.error } ) );
    }
    this.exportLines( job ).forEach( function ( line ) { body.push( line ); } );
    body.push( Dom.el( "div" , { class: "job-actions" , children: actions } ) );

    return Dom.el( "article" , {
      class: "job",
      attrs: { "data-status": job.status },
      children: [ preview , Dom.el( "div" , { class: "job-body" , children: body } ) ],
    } );
  },
};
