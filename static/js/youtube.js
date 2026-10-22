// The YouTube box: paste a video, playlist or channel URL, get one job per
// video. The server looks the URL up before answering, so a playlist's size
// (or a bad URL) is known straight away.
const Youtube = {
  // Always wired up, shown only when enabled: an admin can switch YouTube on
  // in Settings without anyone reloading.
  init( me ) {
    const enabled = Boolean( me.features && me.features.youtube );
    Dom.get( "youtube-form" ).addEventListener( "submit" , this.onSubmit.bind( this ) );
    Dom.show( Dom.get( "youtube-form" ) , enabled );
    Dom.show( Dom.get( "youtube-hint" ) , enabled && I18n.get( "convert.youtube_hint" ) !== "" );
  },

  async onSubmit( event ) {
    event.preventDefault();
    const input = Dom.get( "youtube-url" );
    const button = Dom.get( "youtube-submit" );
    const added = Dom.get( "youtube-added" );
    const url = input.value.trim();
    if ( url === "" ) return;

    button.disabled = true;
    Dom.text( button , I18n.get( "convert.youtube_working" ) );
    Dom.show( added , false );
    try {
      const result = await Api.addYoutube( url );
      const count = result.jobs.length;
      Dom.text( added , count === 1
        ? I18n.format( "convert.youtube_added_one" , { title: result.jobs[ 0 ].title } )
        : I18n.format( "convert.youtube_added_many" , { count: count , batch: result.batch || "" } ) );
      Dom.flash( added , 6000 );
      input.value = "";
      Jobs.refresh();
    } catch ( error ) {
      App.showError( error.message );
    } finally {
      button.disabled = false;
      Dom.text( button , I18n.get( "convert.youtube_button" ) );
    }
  },
};
