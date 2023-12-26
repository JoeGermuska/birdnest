<script>
    export let playlist = {
        image_url: 'https://placekitten.com/200/300',
        date: '2023-12-31'

    };
</script>

<button class='playlist-tile' data-internal-url="">
    <img src="{ playlist.image_url }">
    <h2>
        { playlist.date }
    </h2>
</button>
