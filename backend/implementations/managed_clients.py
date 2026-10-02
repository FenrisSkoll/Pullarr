"""Small protocol adapter factory, not an acquisition orchestrator."""
from backend.implementations.nzbget import NZBGetClient
from backend.implementations.qbittorrent import QBittorrentClient
from backend.implementations.sabnzbd import SABClient


def client_for(config):
    return {'sabnzbd': SABClient, 'nzbget': NZBGetClient, 'qbittorrent': QBittorrentClient}[getattr(config, 'kind', 'sabnzbd')](config)
