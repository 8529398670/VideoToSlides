// The job list: every video this user has sent, what it is doing, and what
// can be done with it.
//
// It polls /api/jobs every couple of seconds while anything is moving (a job
// running, an export building, an upload in flight) and stops when nothing
// is. When a job it has watched turns "ready", it opens the export prompt --
// that is the "convert to PowerPoint and/or Google Slides?" moment.
const Jobs = {
  me: null,
  jobs: [],
  seen: {},          // job id -> last status, to spot the move to "ready"
  deleting: null,    // the job the delete dialog is asking about
  loaded: false,
  timer: null,
  lastRender: "",
  busyInterval: 2000,
  idleInterval: 30000,

  async init( me ) {
    this.me = me;
    this.initDeleteDialog();
    document.addEventListener( "visibilitychange" , function () {
      if ( !document.hidden ) Jobs.refresh();
    } );
    await this.refresh();
    // Back from Google's consent screen: reopen the prompt that sent the
    // person there, or else show the Account view, where the outcome is.
    if ( Google.returned ) {
      const resumed = Google.returned === "connected" && await Export.resumeAfterGoogle( this.jobs );
      if ( !resumed ) {
        App.go( "account" );
      }
    }
  },

  busy() {
    return Upload.busy() || this.jobs.some( function ( job ) {
      return Jobs.isActive( job ) || Jobs.exportWorking( job );
    } );
  },

  isActive( job ) {
    return [ "queued" , "starting" , "downloading" , "scanning" , "extracting" , "filtering" ].indexOf( job.status ) >= 0;
  },

  exportWorking( job ) {
    return Object.values( job.exports || {} ).some( function ( state ) { return state.status === "working"; } );
  },

  async refresh() {
    window.clearTimeout( this.timer );
    let list = null;
    try {
      list = await Api.listJobs();
    } catch ( error ) {
      if ( error.status === 401 ) { window.location.href = "/"; return; }
    }
    if ( list ) {
      const newlyReady = [];
      list.forEach( function ( job ) {
        const before = Jobs.seen[ job.id ];
        // Only jobs seen running on this page prompt: reloading the page
        // must not re-ask about everything that ever finished.
        if ( Jobs.loaded && job.status === "ready" && before !== undefined && before !== "ready" ) newlyReady.push( job );
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
    const snapshot = JSON.stringify( this.jobs );
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

  batch( group ) {
    const ready = group.jobs.filter( function ( job ) { return job.status === "ready" && job.slide_count > 0; } );
    const done = group.jobs.filter( function ( job ) { return !Jobs.isActive( job ); } ).length;
    const convertAll = ready.length < 2 ? null : Dom.el( "button" , {
      class: "small",
      text: I18n.format( "jobs.convert_all" , { count: ready.length } ),
      attrs: { type: "button" },
      on: { click: function () { Export.prompt( ready ); } },
    } );
    return Dom.el( "section" , { class: "batch" , children: [
      Dom.el( "div" , { class: "batch-head" , children: [
        Dom.el( "div" , { children: [
          Dom.el( "h3" , { text: group.batch } ),
          Dom.el( "p" , { class: "muted small-text" , text: I18n.format( "jobs.batch_progress" , { done: done , total: group.jobs.length } ) } ),
        ] } ),
        convertAll,
      ].filter( Boolean ) } ),
      Dom.el( "div" , { class: "batch-jobs" , children: group.jobs.map( function ( job ) { return Jobs.row( job ); } ) } ),
    ] } );
  },

  statusText( job ) {
    if ( job.status === "ready" ) {
      if ( job.slide_count === 0 ) return I18n.get( "jobs.no_slides" );
      // What goes into the deck, not every capture.
      const count = job.included_count;
      return I18n.format( count === 1 ? "jobs.slides_one" : "jobs.slides" , { count: count } );
    }
    const label = I18n.get( "jobs.status_" + job.status ) || job.status;
    if ( [ "downloading" , "scanning" , "extracting" , "filtering" ].indexOf( job.status ) >= 0 ) {
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
    if ( pptx ) {
      if ( pptx.status === "working" ) lines.push( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "export.pptx_working" ) } ) );
      if ( pptx.status === "done" ) lines.push( this.link( "export.pptx_download" , Api.pptxUrl( job ) , { download: "" } ) );
      if ( pptx.status === "failed" ) lines.push( Dom.el( "p" , { class: "notice notice-error" , text: I18n.format( "export.pptx_failed" , { error: pptx.error || "" } ) } ) );
    }
    if ( gslides ) {
      if ( gslides.status === "working" ) lines.push( Dom.el( "p" , { class: "muted small-text" , text: I18n.get( "export.gslides_working" ) } ) );
      if ( gslides.status === "failed" ) lines.push( Dom.el( "p" , { class: "notice notice-error" , text: I18n.format( "export.gslides_failed" , { error: gslides.error || "" } ) } ) );
    }
    // The job's deck, with Delete beside it. The deck outlives a re-run, so
    // this shows whenever one exists and is not being replaced right now.
    if ( this.showsGoogleRow( job ) ) {
      lines.push( Dom.el( "div" , { class: "job-gslides" , children: [
        this.link( "export.gslides_open" , job.gslides_url , { target: "_blank" , rel: "noopener noreferrer" } , "primary" ),
        this.button( "jobs.delete_button" , function () { Jobs.confirmDelete( job ); } ),
      ] } ) );
    }
    return lines;
  },

  showsGoogleRow( job ) {
    const gslides = ( job.exports || {} ).gslides;
    return Boolean( job.gslides_url ) && !( gslides && gslides.status === "working" );
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
    const question = I18n.format( "jobs.delete_confirm" , { title: job.title } );
    if ( !job.gslides_url && question === "" ) {
      this.act( function () { return Api.deleteJob( job.id , false ); } );
      return;
    }
    this.deleting = job;
    Dom.text( Dom.get( "delete-question" ) , question );
    Dom.show( Dom.get( "delete-question" ) , question !== "" );
    Dom.get( "delete-google" ).checked = false;
    Dom.show( Dom.get( "delete-google-row" ) , Boolean( job.gslides_url ) );
    Dom.show( Dom.get( "delete-error" ) , false );
    Dom.get( "delete-go" ).disabled = false;
    Dom.get( "delete-dialog" ).showModal();
  },

  async onDeleteSubmit( event ) {
    event.preventDefault();
    const job = this.deleting;
    if ( !job ) return;
    const deleteGoogle = Boolean( job.gslides_url ) && Dom.get( "delete-google" ).checked;
    const button = Dom.get( "delete-go" );
    button.disabled = true;
    try {
      await Api.deleteJob( job.id , deleteGoogle );
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
    // A job with a Google deck has its Delete next to "Open in Google Slides".
    if ( !this.showsGoogleRow( job ) ) {
      actions.push( this.button( "jobs.delete_button" , function () { Jobs.confirmDelete( job ); } ) );
    }

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
