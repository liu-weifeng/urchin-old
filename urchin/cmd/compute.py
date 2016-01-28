from urchin import service


def main():

    server = service.Service.create('compute')
    service.serve(server)
    service.wait()
