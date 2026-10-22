// Review: every slide a job extracted, as a grid. Clicking a slide leaves it
// out of the deck (and the zip); clicking again brings it back. "View" opens
// it full size, where the arrow keys move between slides and X toggles it.
//
// Exclusions are saved on the server as they are clicked, so closing this
// and exporting later still honours them.
const Review = {
  job: null,
  index: 0,

  init() {
    const dialog = Dom.get( "review-dialog" );
    Dom.get( "review-close" ).addEventListener( "click" , function () { dialog.close(); } );
    Dom.get( "review-convert" ).addEventListener( "click" , function () {
      const job = Review.job;
      dialog.close();
      if ( job ) Export.prompt( [ job ] );
    } );
    dialog.addEventListener( "close" , function () { Jobs.refresh(); } );

    const lightbox = Dom.get( "lightbox" );
    Dom.get( "lightbox-close" ).addEventListener( "click" , function () { lightbox.close(); } );
    Dom.get( "lightbox-prev" ).addEventListener( "click" , function () { Review.step( -1 ); } );
    Dom.get( "lightbox-next" ).addEventListener( "click" , function () { Review.step( 1 ); } );
    Dom.get( "lightbox-toggle" ).addEventListener( "click" , function () { Review.toggle( Review.index ); } );
    lightbox.addEventListener( "keydown" , function ( event ) {
      if ( event.key === "ArrowLeft" ) { event.preventDefault(); Review.step( -1 ); }
      if ( event.key === "ArrowRight" ) { event.preventDefault(); Review.step( 1 ); }
      if ( event.key === "x" || event.key === "X" ) { event.preventDefault(); Review.toggle( Review.index ); }
    } );
    // Clicking the dark backdrop (the dialog itself, not its contents) closes it.
    lightbox.addEventListener( "click" , function ( event ) {
      if ( event.target === lightbox ) lightbox.close();
    } );
  },

  async open( jobId ) {
    let job;
    try {
      job = await Api.getJob( jobId );
    } catch ( error ) {
      App.showError( error.message );
      return;
    }
    this.job = job;
    Dom.text( Dom.get( "review-title" ) , job.title );
    this.renderGrid();
    this.renderCount();
    const dialog = Dom.get( "review-dialog" );
    if ( !dialog.open ) dialog.showModal();
  },

  renderCount() {
    const job = this.job;
    const included = job.slides.filter( function ( slide ) { return !slide.excluded; } ).length;
    job.included_count = included;
    Dom.text( Dom.get( "review-count" ) , I18n.format( "review.count" , { included: included , total: job.slides.length } ) );
    Dom.get( "review-convert" ).disabled = included === 0;
  },

  caption( slide ) {
    return I18n.format( "review.caption" , { n: slide.n , time: I18n.clock( slide.start_sec ) } );
  },

  renderGrid() {
    const grid = Dom.get( "review-grid" );
    Dom.clear( grid );
    this.job.slides.forEach( function ( slide , index ) {
      const tile = Dom.el( "figure" , { class: "slide-tile" + ( slide.excluded ? " is-excluded" : "" ) , children: [
        Dom.el( "button" , {
          class: "tile-image",
          attrs: {
            type: "button",
            "aria-pressed": slide.excluded ? "false" : "true",
            "aria-label": I18n.format( "review.toggle_label" , { n: slide.n } ),
          },
          on: { click: function () { Review.toggle( index ); } },
          children: [
            Dom.el( "img" , { attrs: { src: Api.thumbUrl( Review.job , slide.n ) , alt: "" , loading: "lazy" } } ),
            Dom.el( "span" , { class: "tile-badge" , text: Review.badge( slide ) } ),
          ],
        } ),
        Dom.el( "figcaption" , { children: [
          Dom.el( "span" , { class: "small-text" , text: Review.caption( slide ) } ),
          Dom.el( "button" , {
            class: "secondary small",
            text: I18n.get( "review.view_button" ),
            attrs: { type: "button" },
            on: { click: function () { Review.showLarge( index ); } },
          } ),
        ] } ),
      ] } );
      slide._tile = tile;
      grid.appendChild( tile );
    } );
  },

  async toggle( index ) {
    const slide = this.job.slides[ index ];
    if ( !slide ) return;
    const next = !slide.excluded;
    slide.excluded = next;   // optimistic; put back if the server says no
    this.paint( index );
    try {
      await Api.setExcluded( this.job.id , slide.n , next );
    } catch ( error ) {
      slide.excluded = !next;
      this.paint( index );
      App.showError( error.message );
    }
  },

  // A build step that is left out says why: it is an earlier stage of the
  // slide after it, which shows everything it does.
  badge( slide ) {
    return I18n.get( slide.build_step ? "review.build_badge" : "review.excluded_badge" );
  },

  paint( index ) {
    const slide = this.job.slides[ index ];
    if ( slide._tile ) {
      slide._tile.classList.toggle( "is-excluded" , slide.excluded );
      Dom.text( slide._tile.querySelector( ".tile-badge" ) , this.badge( slide ) );
      slide._tile.querySelector( ".tile-image" ).setAttribute( "aria-pressed" , slide.excluded ? "false" : "true" );
    }
    this.renderCount();
    if ( Dom.get( "lightbox" ).open && index === this.index ) this.renderLarge();
  },

  showLarge( index ) {
    this.index = index;
    this.renderLarge();
    const lightbox = Dom.get( "lightbox" );
    if ( !lightbox.open ) lightbox.showModal();
  },

  step( delta ) {
    const count = this.job.slides.length;
    this.index = ( this.index + delta + count ) % count;
    this.renderLarge();
  },

  renderLarge() {
    const slide = this.job.slides[ this.index ];
    const image = Dom.get( "lightbox-image" );
    image.src = Api.slideUrl( this.job , slide.n );
    image.classList.toggle( "is-excluded" , slide.excluded );
    Dom.text( Dom.get( "lightbox-caption" ) , this.caption( slide ) + ( slide.excluded ? " · " + this.badge( slide ) : "" ) );
    Dom.text( Dom.get( "lightbox-toggle" ) , I18n.get( slide.excluded ? "review.include_button" : "review.exclude_button" ) );
  },
};
