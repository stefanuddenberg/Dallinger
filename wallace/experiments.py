class Experiment(object):
    def __init__(self, session):
        self.task = "Experiment title"
        self.session = session

    def newcomer_arrival_trigger(self, newcomer):
        pass

    def transmission_reception_trigger(self, transmissions):
        # Mark transmissions as received
        for t in transmissions:
            t.mark_received()

    def information_creation_trigger(self, info):
        pass

    def add_sources(self):
        pass

    def step(self):
        pass

    def is_experiment_over(self):
        return all([self.is_network_full(net) for net in self.networks])
