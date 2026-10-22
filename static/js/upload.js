// Getting video files from this computer to the server: drag and drop (files
// or whole folders), or the two pick buttons.
//
// Files go up one at a time, in chunks. Each chunk is its own request and the
// server only accepts one at exactly the offset it has reached, so a network
// hiccup costs one retried chunk, not the whole upload -- and if the server
// says it already has more (a 409 with "received"), we continue from there.
//
// Files dropped as part of a folder carry the folder's name as their "batch",
// which groups them in the job list.
const Upload = {
  me: null,
  extensions: [],
  queue: [],
  current: null,
  maxRetries: 5,

  init( me ) {
    this.me = me;
    this.extensions = ( me.features && me.features.video_extensions ) || [];

    const zone = Dom.get( "drop-zone" );
    const fileInput = Dom.get( "file-input" );
    const folderInput = Dom.get( "folder-input" );
    fileInput.setAttribute( "accept" , [ "video/*" ].concat( this.extensions ).join( "," ) );

    Dom.get( "pick-files" ).addEventListener( "click" , function ( event ) {
      event.stopPropagation();
      fileInput.click();
    } );
    Dom.get( "pick-folder" ).addEventListener( "click" , function ( event ) {
      event.stopPropagation();
      folderInput.click();
    } );
    fileInput.addEventListener( "change" , function () {
      Upload.add( Array.from( fileInput.files ).map( function ( file ) { return { file: file , batch: null }; } ) );
      fileInput.value = "";
    } );
    folderInput.addEventListener( "change" , function () {
      Upload.add( Array.from( folderInput.files ).map( function ( file ) {
        return { file: file , batch: Upload.topFolder( file.webkitRelativePath ) };
      } ) );
      folderInput.value = "";
    } );

    // The zone itself is a big target for keyboards too.
    zone.addEventListener( "keydown" , function ( event ) {
      if ( event.target !== zone ) return;
      if ( event.key === "Enter" || event.key === " " ) {
        event.preventDefault();
        fileInput.click();
      }
    } );

    let depth = 0; // dragenter/leave fire for every child; count to know when we really left
    zone.addEventListener( "dragenter" , function ( event ) {
      event.preventDefault();
      depth += 1;
      zone.classList.add( "is-over" );
    } );
    zone.addEventListener( "dragover" , function ( event ) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    } );
    zone.addEventListener( "dragleave" , function () {
      depth = Math.max( 0 , depth - 1 );
      if ( depth === 0 ) zone.classList.remove( "is-over" );
    } );
    zone.addEventListener( "drop" , async function ( event ) {
      event.preventDefault();
      depth = 0;
      zone.classList.remove( "is-over" );
      const items = await Upload.fromDataTransfer( event.dataTransfer );
      Upload.add( items );
    } );
    // A drop that misses the zone would otherwise navigate the tab to the
    // video file, abandoning everything on the page.
    window.addEventListener( "dragover" , function ( event ) { event.preventDefault(); } );
    window.addEventListener( "drop" , function ( event ) { event.preventDefault(); } );

    window.addEventListener( "beforeunload" , function ( event ) {
      if ( !Upload.busy() ) return;
      event.preventDefault();
      event.returnValue = "";
    } );
  },

  busy() {
    return this.current !== null || this.queue.length > 0;
  },

  topFolder( relativePath ) {
    const parts = String( relativePath || "" ).split( "/" );
    return parts.length > 1 ? parts[ 0 ] : null;
  },

  // Walk dropped folders. getAsEntry has to be read synchronously inside the
  // drop event, before any await, or the browser empties the list.
  async fromDataTransfer( transfer ) {
    const entries = [];
    const loose = [];
    Array.from( transfer.items || [] ).forEach( function ( item ) {
      if ( item.kind !== "file" ) return;
      const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
      if ( entry ) entries.push( entry );
      else if ( item.getAsFile() ) loose.push( { file: item.getAsFile() , batch: null } );
    } );
    if ( entries.length === 0 && loose.length === 0 ) {
      return Array.from( transfer.files || [] ).map( function ( file ) { return { file: file , batch: null }; } );
    }
    const found = loose;
    for ( const entry of entries ) {
      await this.walk( entry , entry.isDirectory ? entry.name : null , found );
    }
    return found;
  },

  async walk( entry , batch , found ) {
    if ( entry.isFile ) {
      const file = await new Promise( function ( resolve ) { entry.file( resolve , function () { resolve( null ); } ); } );
      if ( file ) found.push( { file: file , batch: batch } );
      return;
    }
    if ( !entry.isDirectory ) return;
    const reader = entry.createReader();
    // readEntries returns at most ~100 at a time; keep asking until empty.
    while ( true ) {
      const batchOfEntries = await new Promise( function ( resolve ) {
        reader.readEntries( resolve , function () { resolve( [] ); } );
      } );
      if ( batchOfEntries.length === 0 ) break;
      for ( const child of batchOfEntries ) await this.walk( child , batch , found );
    }
  },

  isVideo( file ) {
    const name = file.name.toLowerCase();
    return this.extensions.some( function ( extension ) { return name.endsWith( extension ); } );
  },

  add( items ) {
    const videos = items.filter( function ( item ) { return Upload.isVideo( item.file ); } );
    const skipped = items.length - videos.length;
    const notice = Dom.get( "skipped-notice" );
    if ( skipped > 0 ) {
      Dom.text( notice , I18n.format( "convert.skipped" , { count: skipped , types: this.extensions.join( " " ) } ) );
      Dom.show( notice , true );
    } else {
      Dom.show( notice , false );
    }
    videos.forEach( function ( item ) {
      item.row = Upload.row( item );
      item.cancelled = false;
      Upload.queue.push( item );
    } );
    if ( videos.length ) this.pump();
  },

  row( item ) {
    const bar = Dom.el( "progress" , { attrs: { max: "100" , value: "0" } } );
    const status = Dom.el( "span" , { class: "muted small-text" , text: I18n.get( "convert.upload_waiting" ) } );
    const cancel = Dom.el( "button" , {
      class: "secondary small",
      text: I18n.get( "convert.upload_cancel" ),
      attrs: { type: "button" },
      on: { click: function () { Upload.cancel( item ); } },
    } );
    const li = Dom.el( "li" , { class: "upload-row" , children: [
      Dom.el( "div" , { class: "upload-name" , children: [
        Dom.el( "span" , { text: item.file.name } ),
        Dom.el( "span" , { class: "muted small-text" , text: I18n.bytes( item.file.size ) } ),
      ] } ),
      bar,
      Dom.el( "div" , { class: "upload-foot" , children: [ status , cancel ] } ),
    ] } );
    Dom.get( "upload-list" ).appendChild( li );
    return { li: li , bar: bar , status: status , cancel: cancel };
  },

  cancel( item ) {
    item.cancelled = true;
    const index = this.queue.indexOf( item );
    if ( index >= 0 ) {
      this.queue.splice( index , 1 );
      item.row.li.remove();
    }
  },

  async pump() {
    if ( this.current ) return;
    while ( this.queue.length ) {
      const item = this.queue.shift();
      this.current = item;
      try {
        await this.send( item );
      } catch ( error ) {
        Dom.text( item.row.status , I18n.format( "convert.upload_failed" , { error: error.message } ) );
        item.row.li.classList.add( "is-failed" );
        item.row.cancel.hidden = true;
      }
      this.current = null;
    }
  },

  sleep( milliseconds ) {
    return new Promise( function ( resolve ) { window.setTimeout( resolve , milliseconds ); } );
  },

  async send( item ) {
    const file = item.file;
    const row = item.row;
    const started = await Api.startUpload( file.name , file.size , item.batch );
    const chunkBytes = started.chunk_bytes;
    let offset = 0;
    let failures = 0;

    while ( offset < file.size ) {
      if ( item.cancelled ) {
        try { await Api.abortUpload( started.id ); } catch ( error ) { /* the purge will catch it */ }
        row.li.remove();
        return;
      }
      try {
        const answer = await Api.uploadChunk( started.id , offset , file.slice( offset , offset + chunkBytes ) );
        offset = answer.received;
        failures = 0;
      } catch ( error ) {
        if ( error.status === 409 && typeof error.received === "number" ) {
          offset = error.received;
          continue;
        }
        // A 4xx other than that will not get better by retrying.
        if ( error.status && error.status >= 400 && error.status < 500 && error.status !== 429 ) throw error;
        failures += 1;
        if ( failures > this.maxRetries ) throw error;
        Dom.text( row.status , I18n.format( "convert.upload_retrying" , { attempt: failures } ) );
        await this.sleep( 1000 * failures );
        continue;
      }
      const percent = Math.floor( ( offset / file.size ) * 100 );
      row.bar.value = percent;
      Dom.text( row.status , I18n.format( "convert.upload_progress" , {
        done: I18n.bytes( offset ) , total: I18n.bytes( file.size ) , percent: percent ,
      } ) );
    }

    row.cancel.hidden = true;
    Dom.text( row.status , I18n.get( "convert.upload_finishing" ) );
    await Api.completeUpload( started.id );
    row.bar.value = 100;
    Dom.text( row.status , I18n.get( "convert.upload_done" ) );
    row.li.classList.add( "is-done" );
    window.setTimeout( function () { row.li.remove(); } , 2500 );
    Jobs.refresh();
  },
};
